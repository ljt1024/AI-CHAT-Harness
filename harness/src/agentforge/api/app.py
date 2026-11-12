"""Application assembly.

This is the only place that knows how a request turns into an answer end to
end: which store, which gateway, which pool, and in what order the middlewares
and routers go on. Everything it builds is one `AppDeps` object, so the routers
next door never reach for a global.
"""
from collections import OrderedDict
import contextvars

from fastapi import FastAPI

from agentforge import session_files
from agentforge.api.deps import AppDeps
from agentforge.api.lifespan import build_lifespan
from agentforge.api.middleware import build_authenticate, log_request
from agentforge.api.routes import agents, auth as auth_routes, chat, files, health, mcp, runs
from agentforge.api.routes import sandbox as sandbox_routes, skills as skill_routes
from agentforge.auth import AuthService
from agentforge.config import Settings
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.registry import ModelGateway
from agentforge.logging_config import configure
from agentforge.mail import build_mailer
from agentforge.permissions import ApprovalManager
from agentforge.policy import InterventionPolicy
from agentforge.sandbox.docker_runner import DockerRunner
from agentforge.sandbox.pool import SandboxPool
from agentforge.skills import SkillManager
from agentforge.sms import build_sender
from agentforge.store import MongoDBStore


def create_app(settings=None, store=None, gateway=None):
    settings = settings or Settings()
    configure()
    store = store if store is not None else MongoDBStore(settings.mongodb_uri, settings.agentforge_db)
    gateway = gateway or ModelGateway(retries=settings.model_retries,
                                      backoff=settings.model_retry_backoff,
                                      max_backoff=settings.model_retry_max_backoff)
    if "deepseek" not in gateway.adapters:
        gateway.register("deepseek", DeepSeekAdapter(settings.deepseek_api_key.get_secret_value()))
    pool = SandboxPool(DockerRunner(settings.agentforge_workspace / "sandboxes"),
        warm_size=settings.sandbox_warm_size, max_size=settings.sandbox_max_size,
        idle_seconds=settings.sandbox_idle_seconds,
        max_per_owner=settings.sandbox_max_per_owner)
    auth = AuthService(store, session_hours=settings.session_hours,
                       code_ttl=settings.verify_code_ttl,
                       cooldown=settings.verify_cooldown,
                       per_address=settings.verify_daily_per_address,
                       per_ip=settings.verify_daily_per_ip)
    # None when the provider is named but not configured. The endpoints answer
    # 503 rather than silently printing codes into the log.
    sms = build_sender(settings)
    mail = build_mailer(settings)
    # Blobs live beside the workspaces, not inside one: a sandbox is destroyed
    # when its lease is released, and the reader must still be able to download.
    file_store = session_files.SessionFiles(
        settings.agentforge_workspace / "_uploads",
        settings.upload_max_bytes, settings.upload_max_files,
        settings.upload_max_bytes_per_owner)
    policy = InterventionPolicy.load(settings.intervention_policy)
    approvals = ApprovalManager(store, settings.approval_timeout, policy)

    deps = AppDeps(
        settings=settings, store=store, gateway=gateway, pool=pool, auth=auth,
        sms=sms, mail=mail, file_store=file_store,
        skill_manager=SkillManager(store), approvals=approvals, policy=policy,
        runs={}, finished_runs=OrderedDict(), session_owners={},
        isolated_tools={}, run_sandboxes={},
        # Carries the session for the approval check: the runtime only passes
        # (agent, tool, call, emit), so the pending approval learns which run
        # asked from this.
        current_session=contextvars.ContextVar("agentforge_session", default=""),
    )

    app = FastAPI(title="AgentForge", lifespan=build_lifespan(deps))
    deps.app = app
    # Set here as well as in the lifespan: a request that arrives before startup
    # finishes must see "not ready" rather than an AttributeError.
    app.state.ready = False
    app.state.runtime, app.state.approvals = deps.runtime, approvals
    app.state.sandbox_pool = pool
    # Exposed so operators (and the runtime status endpoint) can inspect what is running.
    app.state.runs, app.state.finished_runs = deps.runs, deps.finished_runs
    app.state.auth = auth
    app.state.sms = sms
    app.state.mail = mail

    # Order matters and is the reason these two are not decorators here:
    # Starlette wraps in reverse, so the logger added last is outermost and a
    # 401 still carries X-Request-Id.
    app.middleware("http")(build_authenticate(deps))
    app.middleware("http")(log_request)

    for module in (auth_routes, health, agents, runs, files, skill_routes, mcp,
                   sandbox_routes, chat):
        app.include_router(module.build_router(deps))

    return app
