import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import contextvars
import json
import logging
import re
import time
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from agentforge.config import Settings
from agentforge.artifacts import ArtifactEventBus
from agentforge.auth import (GUEST_COOKIE, SESSION_COOKIE, AuthError, AuthService,
                             SmsCodeError, current_user, is_guest, new_guest_id,
                             valid_guest_id)
from agentforge.capabilities import (MCPServerRequest, MAX_CONFIG, parse_config, parse_servers,
                                     public_server, run_servers, server_tools)
from agentforge.context import ContextManager
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.base import ModelOutputError
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime
from agentforge.logging_config import configure, request_id
from agentforge.redact import describe, mask_url, status_code
from agentforge.middleware import MiddlewareStack
from agentforge.models import DEFAULT_AGENT_ID, PYTHON_SANDBOX_IMAGES, AgentDefinition, ModelRef, new_id
from agentforge.permissions import ApprovalManager
from agentforge.policy import InterventionPolicy
from agentforge.prompts import with_response_language
from agentforge.store import MongoDBStore
from agentforge import email_address
from agentforge import phone as phone_number
from agentforge import session_files
from agentforge.mail import build_mailer
from agentforge.sms import build_sender
from agentforge.tools.ask import AskUserTool
from agentforge.tools.base import ToolRegistry
from agentforge.tools.ecommerce import ECOM_TOOL_MAP, ECOM_TOOLS
from agentforge.tools.fs import FileTool, Workspace
from agentforge.tools.shell import ShellTool
from agentforge.tools.web import WEB_TOOL_MAP, WEB_TOOLS
from agentforge.sandbox.docker_runner import DockerRunner, SandboxUnavailable
from agentforge.sandbox.pool import SandboxPool
from agentforge.sandbox.tools import SandboxTool
from agentforge.models import SandboxPolicy
from agentforge.skills import (SKILL_TOOL_NAMES, SkillManager, SkillPackage, SkillTool,
    remove_synced_skill)

logger = logging.getLogger(__name__)

# The only route that receives a file body. Bounding it in the HTTP middleware
# matters because Starlette spools the whole multipart form before the endpoint
# runs, so a limit applied inside the endpoint is already too late.
UPLOAD_PATH = re.compile(r"^/api/sessions/[^/]+/files$")


class LegacyRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100000)
    sessionId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turnId: str = Field(default_factory=new_id, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    stream: bool = False
    # Continue the previous run from its latest checkpoint instead of a fresh turn.
    resume: bool = False
    agentIds: list[str] = Field(default_factory=list, max_length=1)
    model: str = "deepseek-chat"
    fileIds: list[str] = Field(default_factory=list)
    sandbox: bool = False
    skills: list[str] = Field(default_factory=list, max_length=8)
    mcp_servers: list[str] = Field(default_factory=list, max_length=8)


class SessionRequest(BaseModel):
    agent_id: str
    task: str = Field(min_length=1, max_length=100000)


class ApprovalRequest(BaseModel):
    approved: StrictBool
    reason: str = Field(default="", max_length=2000)
    # "Yes, and stop asking about this tool for the rest of this conversation."
    # Ignored for tools the gate does not consider rememberable.
    remember: bool = False


class MCPConfig(BaseModel):
    """A pasted `{"mcpServers": {...}}` block, unparsed."""
    config: str = Field(min_length=1, max_length=MAX_CONFIG)


class MCPToggle(BaseModel):
    enabled: bool


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class SmsCodeRequest(BaseModel):
    """Ask for a code. `bind` is only honoured for a signed-in caller."""

    phone: str = Field(min_length=6, max_length=24)
    purpose: str = Field(default="login", pattern=r"^(login|bind)$")


class SmsLoginRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=12)


class PhoneBindRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=12)


class EmailCodeRequest(BaseModel):
    """Ask for a code by email. `bind` is only honoured for a signed-in caller."""

    email: str = Field(min_length=5, max_length=254)
    purpose: str = Field(default="login", pattern=r"^(login|bind)$")


class EmailLoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class EmailBindRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class SandboxFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=8_000_000)


class SandboxExecRequest(BaseModel):
    """One-shot execution inside a disposable sandbox.

    Lets host-side tooling run untrusted code with the agent's isolation
    instead of the host's privileges: offline, read-only root, no capabilities.
    """

    command: list[str] = Field(min_length=1, max_length=64)
    files: list[SandboxFile] = Field(default_factory=list, max_length=200)
    timeout: float = Field(default=30, gt=0, le=600)
    image: str = Field(default="python:3.12-slim", pattern=r"^[a-zA-Z0-9._:/@-]{1,200}$")


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(max_length=100000)


class CompletionRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    model: str = "deepseek-chat"
    stream: bool = True


def legacy_result(result, task):
    return {"id": result["trace_id"], "sessionId": result["session_id"], "input": task,
            "output": result["output"], "steps": [], "memoryMessages": 0,
            "cost": result["cost"], "stop_reason": result["stop_reason"],
            "artifacts": result.get("artifacts", [])}


# Internal name -> name the browser receives. Kept as a table rather than scattered
# reassignments so the mapping is checkable against WIRE_EVENTS (test_event_contract).
LEGACY_EVENT_NAMES = {"turn_started": "start", "content": "delta", "tool_call": "step",
                      "tool_result": "step", "model_recovery": "step"}


def legacy_event(event, task):
    event = event.copy()
    kind = event["type"]
    event["type"] = LEGACY_EVENT_NAMES.get(kind, kind)
    if kind == "tool_call":
        call = event["call"]
        event["step"] = {"id": call["id"], "toolCallId": call["id"], "phase": "action",
                         "status": "running", "agentId": call["name"],
                         "output": f"{call['name']} {json.dumps(call['arguments'])}"}
    elif kind == "tool_result":
        result = event["result"]
        event["step"] = {"id": event["call_id"], "toolCallId": event["call_id"],
                         "phase": "observation", "status": "completed" if result["ok"] else "failed",
                         "output": result["output"] or result["error"] or ""}
    elif kind == "model_recovery":
        event["step"] = {"id": event["step_id"], "phase": "thought",
                         "status": "completed", "output": event["message"]}
    elif kind == "done":
        event["result"] = legacy_result(event["result"], task)
    return event


async def event_frames(bus, task="", legacy=False, after=0):
    cursor = after
    while True:
        async with bus.condition:
            try:
                await asyncio.wait_for(bus.condition.wait_for(lambda: len(bus.events) > cursor), 15)
            except TimeoutError:
                batch = []
            else:
                batch = bus.events[cursor:]
        if not batch:
            yield ": heartbeat\n\n"
        for event in batch:
            cursor += 1
            if legacy:
                event = legacy_event(event, task)
            yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] in {"done", "error"}:
                return


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
    isolated_tools = {}
    run_sandboxes = {}
    skill_manager = SkillManager(store)
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

    def tools_factory(session_id):
        if session_id in isolated_tools:
            return isolated_tools[session_id]
        # The session id becomes a directory name here, so it is validated at
        # the door rather than assumed to be the uuid the browser normally sends.
        workspace = Workspace(settings.agentforge_workspace / session_files.session_identity(session_id))
        registry = ToolRegistry()
        for name in ("read_file", "write_file", "grep", "glob"):
            registry.register(FileTool(workspace, name))
        registry.register(ShellTool(workspace))
        registry.register(AskUserTool())
        for tool in WEB_TOOLS:
            registry.register(tool())
        for tool in ECOM_TOOLS:
            registry.register(tool(settings))
        for name in SKILL_TOOL_NAMES:
            registry.register(SkillTool(skill_manager, name))
        return registry

    runtime = Runtime(gateway, store, tools_factory)
    runtime.middleware, runtime.context_manager = MiddlewareStack(), ContextManager()
    async def sync_skills(ctx):
        if ctx.agent.skills:
            sandbox = run_sandboxes[ctx.metadata["session_id"]]
            ctx.metadata["skills"] = await skill_manager.sync(ctx.agent.skills, sandbox)
    skills_layer = next(layer for layer in runtime.middleware.layers if layer.name == "SkillsSync")
    skills_layer.sync, skills_layer.enabled = sync_skills, True
    policy = InterventionPolicy.load(settings.intervention_policy)
    approvals = ApprovalManager(store, settings.approval_timeout, policy)
    # The runtime only passes (agent, tool, call, emit), so carry the session on a
    # context var to associate a pending approval with the run that requested it.
    current_session = contextvars.ContextVar("agentforge_session", default="")

    async def permission_check(agent, tool, call, emit):
        return await approvals.check(agent, tool, call, emit, session_id=current_session.get())

    runtime.permission_check = permission_check
    runs = {}
    # Session id -> account that started it. Runs and finished-run buses live in
    # memory, so this is what lets the SSE, stop and delete endpoints tell the
    # caller's session from a stranger's without a database round trip.
    session_owners = {}
    # Finished runs keep their event log briefly so a reconnecting SSE client can
    # replay the tail it missed. Bounded, otherwise long-lived processes would
    # retain every event log forever.
    finished_runs = OrderedDict()
    FINISHED_RUN_LIMIT = 16

    def retire_run(session_id, task, bus):
        if runs.get(session_id, (None, None))[1] is not task:
            return
        runs.pop(session_id, None)
        finished_runs[session_id] = bus
        finished_runs.move_to_end(session_id)
        while len(finished_runs) > FINISHED_RUN_LIMIT:
            stale, _ = finished_runs.popitem(last=False)
            session_owners.pop(stale, None)

    def lookup_run(session_id):
        """Resolve a session to (bus, terminal) from active runs then finished ones."""
        if session_id in runs:
            bus, running = runs[session_id]
            return bus, running.done()
        bus = finished_runs.get(session_id)
        if bus is None:
            return None
        finished_runs.move_to_end(session_id)
        return bus, True

    default = AgentDefinition(id="default", name="AgentForge",
        description="通用助手：可以读写工作区文件、运行受限命令、搜索并读取网页、查询商品价格、按需加载技能。",
        tools=["read_file", "write_file", "grep", "glob", "run_command", "ask_user",
               "web_search", "web_fetch", "search_products", "get_product"])

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        try:
            await store.setup()
            await pool.start()
            await auth.setup()
            await auth.prune()
            await auth.prune_guests(settings.guest_retention_hours)
            owner = await auth.bootstrap(settings.admin_user,
                                         settings.admin_password.get_secret_value())
            # An installation can hold accounts while the environment stopped
            # naming one (a developer's machine, an operator who rotated the
            # file). Legacy rows still need an owner, or they stay invisible to
            # everyone -- the same failure as the built-in agent below.
            operator = owner["id"] if owner else await auth.first_owner()
            if operator is None:
                logger.warning("No account exists yet. Set ADMIN_USER and ADMIN_PASSWORD, then "
                               "restart: every /api request is refused until then.")
            else:
                if owner is None:
                    logger.warning("ADMIN_USER is not configured; binding legacy resources to "
                                   "the first account (%s).", operator)
                # Resources written before accounts existed belong to the account
                # the operator designated, not to whoever logs in first. The
                # built-in agent is excluded there on purpose.
                await auth.adopt_ownerless(operator)
            # The built-in agent is shared: its empty owner means every account,
            # visitor included, may read and run it, and nobody may replace it.
            # So exactly one row carries that id. An earlier migration bound it
            # to an account, which reads fine for that account and empties the
            # catalogue for everyone else, and no API path can create another
            # row with this id -- anything private under it is that leftover.
            orphaned = await store.db.agents.delete_many(
                {"id": DEFAULT_AGENT_ID, "owner_id": {"$ne": ""}})
            if orphaned.deleted_count:
                logger.warning("Dropped %d private copy of the built-in agent; it is "
                               "shared read-only.", orphaned.deleted_count)
            await store.put("agents", default.model_dump(), owner="")
            # MCP servers belong to an account, so a row with an empty owner is a
            # leftover from a boot that had no account yet -- the config file is
            # the only thing that plants them. Left alone it reads as shared, and
            # the next config refresh inserts an owned twin with the same id; the
            # list then carries the server twice, discovery runs twice, and every
            # save that validates an MCP-backed agent dies on "Duplicate tool".
            # Bind the only copy, drop the stale twin.
            if operator:
                shared = await store.db.mcp_servers.find({"owner_id": ""}).to_list(length=10000)
                for row in shared:
                    owned = await store.db.mcp_servers.count_documents(
                        {"id": row["id"], "owner_id": operator})
                    if owned:
                        await store.db.mcp_servers.delete_one({"_id": row["_id"]})
                    else:
                        await store.db.mcp_servers.update_one(
                            {"_id": row["_id"]}, {"$set": {"owner_id": operator}})
                if shared:
                    logger.info("Settled %d shared MCP rows onto %s", len(shared), operator)
            # The startup MCP file is planted by the operator, so its rows belong
            # to that account rather than to nobody in particular. Seeding under
            # the operator (not the configured `owner`, which can be absent while
            # accounts exist) is what stops the rows coming back ownerless.
            planted = current_user.set(operator) if operator else None
            try:
                await load_mcp_config(settings.mcp_config)
            finally:
                if planted is not None:
                    current_user.reset(planted)
            app.state.ready = True
        except Exception:
            logger.warning("MongoDB unavailable; data endpoints return 503.", exc_info=True)
        yield
        for _, task in runs.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in runs.values()), return_exceptions=True)
        await pool.close()

    app = FastAPI(title="AgentForge", lifespan=lifespan)
    # Set here as well as in the lifespan: a request that arrives before startup
    # finishes must see "not ready" rather than an AttributeError.
    app.state.ready = False
    app.state.runtime, app.state.approvals = runtime, approvals
    app.state.sandbox_pool = pool
    # Exposed so operators (and the runtime status endpoint) can inspect what is running.
    app.state.runs, app.state.finished_runs = runs, finished_runs
    app.state.auth = auth
    app.state.sms = sms
    app.state.mail = mail

    # The only routes reachable without a session. Everything else under /api is
    # refused before it reaches a handler, so a new endpoint is protected by
    # default instead of by remembering to add a check.
    PUBLIC_API = {"/api/auth/login", "/api/auth/logout",
                  "/api/auth/sms/code", "/api/auth/sms/verify",
                  "/api/auth/email/code", "/api/auth/email/verify"}

    # What an anonymous visitor may reach: reading the catalogue, running a real
    # turn, and answering the approval that turn raised. Anything that changes
    # the installation (a new agent, a skill, an MCP server, a sandbox lease) is
    # deliberately absent, so it answers 401 and the interface asks to sign in.
    GUEST_ROUTES = {
        ("GET", "/api/models"), ("GET", "/api/agents"),
        ("POST", "/api/agents/run"), ("POST", "/api/sessions"),
        ("POST", "/api/chat/completions"),
        ("GET", "/api/approvals"), ("GET", "/api/interventions/policy"),
    }

    def guest_allowed(method: str, path: str) -> bool:
        if method in {"GET", "HEAD"} and path.startswith(("/api/sessions/", "/api/files/")):
            return True
        if method == "POST" and path.startswith(("/api/sessions/", "/api/approvals/")):
            # Stopping a run and answering its approval. Deleting a session is a
            # DELETE and stays behind the wall.
            return True
        return (method, path) in GUEST_ROUTES

    async def bill_guest_turn():
        """Charge an anonymous turn to the visitor, before the model is called.

        Raises 403 with a machine-readable reason so the interface can open the
        login dialog instead of showing a generic failure.
        """
        identity = current_user.get()
        if not is_guest(identity):
            return
        refusal = await auth.claim_guest_turn(identity, settings.guest_turns,
                                              settings.guest_daily_turns)
        if refusal:
            raise HTTPException(403, detail={"code": "guest_limit", "reason": refusal})

    def owner_id() -> str:
        """The calling account, from the cookie alone.

        Raises rather than returning a default: an empty identity here would
        silently turn an ownership filter into "everything", which is the exact
        failure this design is meant to prevent.
        """
        identity = current_user.get()
        if not identity:
            raise HTTPException(401, "Not authenticated.")
        return identity

    async def owned_session(identity: str) -> dict:
        """A session the caller owns, or the same 404 an unknown id gets.

        Answering 403 here would confirm that somebody else's session exists,
        which is exactly what a probing caller wants to learn.
        """
        session = await store.get("sessions", identity, owner=owner_id())
        if session is None:
            raise HTTPException(404, "Session not found.")
        return session

    def same_origin(request: Request) -> bool:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if not origin:
            # Browsers always send Origin on unsafe methods; curl and the test
            # client do not, and neither carries an ambient cookie from another site.
            return True
        allowed = {item.strip().rstrip("/")
                   for item in settings.allowed_origins.split(",") if item.strip()}
        if origin in allowed:
            return True
        # Behind a reverse proxy the Host header is the upstream, and the browser's
        # own hostname arrives as X-Forwarded-Host. Both are legitimate answers to
        # "which site is this request for".
        hosts = {request.headers.get("host", ""),
                 (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()}
        return origin.split("://", 1)[-1] in {host for host in hosts if host}

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        # Static assets are served by nginx or Vite; only the API is ours to guard.
        if not path.startswith("/api"):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        try:
            user = await auth.user_for_token(token) if token else None
        except Exception:
            logger.warning("Session lookup failed; treating the request as anonymous.",
                           exc_info=True)
            user = None

        # A visitor without a session is not turned away at the door: it is given
        # an identity of its own so the ownership filters work unchanged, and only
        # the routes in GUEST_ROUTES are reachable through it.
        guest, issued = "", ""
        if user is None and path not in PUBLIC_API:
            permitted = path == "/api/auth/me" or (
                settings.guest_turns > 0 and guest_allowed(request.method, path))
            if not permitted:
                return JSONResponse({"detail": "Not authenticated.",
                                     "code": "login_required"}, status_code=401)
            candidate = request.cookies.get(GUEST_COOKIE, "")
            guest = candidate if valid_guest_id(candidate) else new_guest_id()
            issued = "" if valid_guest_id(candidate) else guest

        identity = user["id"] if user else guest
        claim = current_user.set(identity) if identity else None
        try:
            # Applies to every unsafe method, signed in or not: a form posted
            # from another site would otherwise be able to sign a visitor into
            # an account the attacker owns (login CSRF), which a check gated on
            # "has an identity already" cannot see.
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(request):
                return JSONResponse({"detail": "Cross-origin request rejected."},
                                    status_code=403)
            # An upload is the one body worth bounding before it is read:
            # Starlette spools the whole multipart form to disk before the
            # handler runs, so a check inside the endpoint would already have
            # paid for the bytes. Answers the same 413 the endpoint would.
            if request.method == "POST" and UPLOAD_PATH.fullmatch(path):
                declared = request.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > settings.upload_max_bytes + 65_536:
                    return JSONResponse(
                        {"detail": f"File exceeds the {settings.upload_max_bytes // (1024 * 1024)} MB limit."},
                        status_code=413)
            if user is not None:
                request.state.user = user
            if guest:
                request.state.guest = guest
            response = await call_next(request)
        finally:
            if claim is not None:
                current_user.reset(claim)
        if issued:
            # A week is long enough to be worth keeping and short enough that a
            # stale anonymous transcript is not defended forever; the daily
            # counter resets on its own, but the transcript does not.
            response.set_cookie(GUEST_COOKIE, issued, max_age=7 * 24 * 3600,
                                httponly=True, samesite="lax",
                                secure=settings.cookie_secure, path="/")
        return response

    def public_user(user: dict) -> dict:
        """What the browser may know about an account.

        The address and the number leave the server masked: the interface never
        needs either in full to show who is signed in, and the full value is
        exactly what a cross-site script would be after.
        """
        return {"id": user["id"], "username": user["username"],
                "displayName": user.get("display_name") or user["username"],
                "email": email_address.mask(user["email"]) if user.get("email") else "",
                "phone": phone_number.mask(user["phone"]) if user.get("phone") else ""}

    async def finish_login(user: dict, request: Request, response: Response):
        """Everything that happens once we know who is calling.

        One place, because a password login and a code login differ only in how
        they got here: both start a session, and both carry the visitor's trial
        transcript over instead of dropping it. Failing to move the transcript
        must not fail the login -- the account exists and the cookie is set.
        """
        token = await auth.start_session(user["id"])
        response.set_cookie(SESSION_COOKIE, token, max_age=int(settings.session_hours * 3600),
                            httponly=True, samesite="lax", secure=settings.cookie_secure,
                            path="/")
        guest = request.cookies.get(GUEST_COOKIE, "")
        if valid_guest_id(guest):
            try:
                await auth.adopt_guest(guest, user["id"])
                # The transcript may already own a sandbox from a pre-login turn.
                # Move the lease with the rows, or the next run refuses to resume
                # onto work this reader actually did.
                moved = await pool.adopt_owner(guest, user["id"])
                if moved:
                    logger.info("Re-bound %d sandbox lease(s) from %s to %s",
                                moved, guest, user["id"])
            except Exception:
                logger.warning("Could not adopt guest transcript %s", guest, exc_info=True)
            response.delete_cookie(GUEST_COOKIE, path="/")
        return {"code": 200, "data": public_user(user)}

    def client_ip(request: Request) -> str:
        """The caller's address, for the per-address send budget.

        Behind nginx the socket address is the proxy, so the forwarded header is
        the only thing that distinguishes two visitors. It is client-controlled
        and therefore only a budget key, never an authorisation.
        """
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "")

    def require_ready():
        if not app.state.ready:
            raise HTTPException(503, "MongoDB unavailable. Start docker compose up -d mongo and "
                                     "restart Harness.")

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response):
        require_ready()
        try:
            user = await auth.authenticate(payload.username, payload.password)
        except AuthError as exc:
            logger.info("login failed user=%s", payload.username[:32])
            raise HTTPException(401, str(exc)) from None
        logger.info("login user=%s", user["username"])
        return await finish_login(user, request, response)

    @app.post("/api/auth/sms/code")
    async def send_code(payload: SmsCodeRequest, request: Request):
        """Send a sign-in code, or a binding code to a signed-in account.

        Registration and sign-in share one endpoint because they are one flow:
        whether this number is new is not something the sender should have to
        know before asking, and telling it here would turn the endpoint into a
        way to test whether somebody has an account.
        """
        require_ready()
        try:
            number = phone_number.normalize(payload.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        user = getattr(request.state, "user", None)
        if payload.purpose == "bind":
            if user is None:
                raise HTTPException(401, detail={"code": "login_required"})
            bound = await auth.user_by_phone(number)
            if bound and bound["id"] != user["id"]:
                raise HTTPException(409, detail={"code": "phone_taken"})
        else:
            # Do not spend a message on a number that cannot become an account.
            if not settings.signup_open and await auth.user_by_phone(number) is None:
                raise HTTPException(403, detail={"code": "signup_closed"})
        if sms is None:
            raise HTTPException(503, detail={"code": "sms_unavailable"})
        try:
            sent = await auth.issue_code(number, payload.purpose, client_ip(request), sms,
                                         channel="sms")
        except SmsCodeError as exc:
            status = 429 if exc.reason in {"cooldown", "daily_limit"} else 502
            raise HTTPException(status, detail={"code": exc.reason,
                                               "retryAfter": exc.retry_after}) from None
        data = {"expiresIn": sent["expiresIn"], "cooldown": sent["cooldown"]}
        if getattr(sms, "echo", False):
            # Console channel with echo on: lets a tester finish signing in
            # without a phone. `build_sender` refuses to do this quietly.
            data["debugCode"] = sent["code"]
        logger.info("sms code sent phone=%s purpose=%s", phone_number.mask(number),
                    payload.purpose)
        return {"code": 200, "data": data}

    @app.post("/api/auth/sms/verify")
    async def verify_sms(payload: SmsLoginRequest, request: Request, response: Response):
        """Sign in with a code; an unknown number becomes a new account."""
        require_ready()
        try:
            number = await auth.verify_code(payload.phone, "login", payload.code,
                                            channel="sms")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        user = await auth.user_by_phone(number)
        if user is None:
            if not settings.signup_open:
                raise HTTPException(403, detail={"code": "signup_closed"})
            user = await auth.create_phone_user(number)
        logger.info("login by phone=%s", phone_number.mask(number))
        return await finish_login(user, request, response)

    @app.post("/api/auth/phone")
    async def bind_phone(payload: PhoneBindRequest, request: Request):
        """Attach a number to an existing account (the one `bootstrap()` made)."""
        require_ready()
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, detail={"code": "login_required"})
        try:
            number = await auth.verify_code(payload.phone, "bind", payload.code,
                                            channel="sms")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        try:
            updated = await auth.bind_phone(user["id"], number)
        except SmsCodeError as exc:
            raise HTTPException(409, detail={"code": exc.reason}) from None
        return {"code": 200, "data": public_user(updated)}

    @app.post("/api/auth/email/code")
    async def send_email_code(payload: EmailCodeRequest, request: Request):
        """Send a sign-in code, or a binding code to a signed-in account.

        Same shape as the SMS door, on purpose: registration and sign-in are one
        flow, and asking "is this address new" before sending would turn the
        endpoint into an account-enumeration oracle.
        """
        require_ready()
        try:
            address = email_address.normalize(payload.email)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        user = getattr(request.state, "user", None)
        if payload.purpose == "bind":
            if user is None:
                raise HTTPException(401, detail={"code": "login_required"})
            bound = await auth.user_by_email(address)
            if bound and bound["id"] != user["id"]:
                raise HTTPException(409, detail={"code": "email_taken"})
        else:
            # Do not spend a message on an address that cannot become an account.
            if not settings.signup_open and await auth.user_by_email(address) is None:
                raise HTTPException(403, detail={"code": "signup_closed"})
        if mail is None:
            raise HTTPException(503, detail={"code": "mail_unavailable"})
        try:
            sent = await auth.issue_code(address, payload.purpose, client_ip(request),
                                         mail, channel="email")
        except SmsCodeError as exc:
            status = 429 if exc.reason in {"cooldown", "daily_limit"} else 502
            raise HTTPException(status, detail={"code": exc.reason,
                                               "retryAfter": exc.retry_after}) from None
        data = {"expiresIn": sent["expiresIn"], "cooldown": sent["cooldown"]}
        if getattr(mail, "echo", False):
            # Console channel with echo on: lets a tester finish signing in
            # without a mailbox. `build_mailer` refuses to do this quietly.
            data["debugCode"] = sent["code"]
        logger.info("email code sent address=%s purpose=%s",
                    email_address.mask(address), payload.purpose)
        return {"code": 200, "data": data}

    @app.post("/api/auth/email/verify")
    async def verify_email_code(payload: EmailLoginRequest, request: Request,
                                response: Response):
        """Sign in with a code; an unknown address becomes a new account."""
        require_ready()
        try:
            address = await auth.verify_code(payload.email, "login", payload.code,
                                            channel="email")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        user = await auth.user_by_email(address)
        if user is None:
            if not settings.signup_open:
                raise HTTPException(403, detail={"code": "signup_closed"})
            user = await auth.create_email_user(address)
        logger.info("login by email=%s", email_address.mask(address))
        return await finish_login(user, request, response)

    @app.post("/api/auth/email")
    async def bind_email(payload: EmailBindRequest, request: Request):
        """Attach an address to an existing account (the one `bootstrap()` made)."""
        require_ready()
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, detail={"code": "login_required"})
        try:
            address = await auth.verify_code(payload.email, "bind", payload.code,
                                             channel="email")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        try:
            updated = await auth.bind_email(user["id"], address)
        except SmsCodeError as exc:
            raise HTTPException(409, detail={"code": exc.reason}) from None
        return {"code": 200, "data": public_user(updated)}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        await auth.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"code": 200, "data": {"loggedOut": True}}

    @app.get("/api/auth/me")
    async def whoami(request: Request):
        """Who is calling, plus how much trial is left.

        Always public: a page has to be able to ask "am I signed in" before it
        can decide anything, and the anonymous budget has to be readable without
        spending a turn.
        """
        user = getattr(request.state, "user", None)
        if user is not None:
            return {"code": 200, "data": public_user(user),
                    "guest": {"allowed": False, "turnsLeft": 0, "turnsTotal": 0}}
        guest = getattr(request.state, "guest", "")
        try:
            budget = await auth.guest_budget(guest, settings.guest_turns,
                                             settings.guest_daily_turns)
        except Exception:
            logger.warning("Guest budget lookup failed.", exc_info=True)
            budget = {"allowed": False, "turnsLeft": 0, "turnsTotal": 0}
        return {"code": 200, "data": None, "guest": budget}

    @app.middleware("http")
    async def log_request(request, call_next):
        # The id is echoed in the response header so a user reporting a problem
        # can quote one value and have every related record filtered out. It is
        # honoured when a caller supplies its own, so a browser trace and the
        # server log can share it.
        supplied = request.headers.get("X-Request-Id", "").strip()
        rid = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else new_id()[:12]
        token = request_id.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("%s %s failed after %.0fms", request.method, request.url.path,
                             (time.perf_counter() - started) * 1000)
            raise
        else:
            elapsed = (time.perf_counter() - started) * 1000
            response.headers["X-Request-Id"] = rid
            # Health is polled constantly and says nothing on success.
            emit_log = logger.debug if request.url.path == "/health" else logger.info
            emit_log("%s %s -> %s %.0fms", request.method, request.url.path,
                     response.status_code, elapsed)
            return response
        finally:
            request_id.reset(token)

    def ready(model=False):
        if not app.state.ready:
            raise HTTPException(503, "MongoDB unavailable. Start docker compose up -d mongo and restart Harness.")
        adapter = gateway.adapters.get("deepseek")
        if model and isinstance(adapter, DeepSeekAdapter) and not adapter.api_key:
            raise HTTPException(503, "Configure the DeepSeek API key in the Harness environment and restart.")

    @app.get("/health")
    async def health():
        # Probe the three things a run actually depends on. Reporting "ok" on the
        # database alone hid a missing model key or a dead Docker daemon until a
        # run failed, which made the endpoint useless for triage.
        adapter = gateway.adapters.get("deepseek")
        model_ready = bool(getattr(adapter, "api_key", None))
        try:
            async with asyncio.timeout(3):
                await pool.runner.connect()
            docker_ready = True
        except Exception:
            docker_ready = False
        return {"status": "ok" if app.state.ready and model_ready else "degraded",
                "database": app.state.ready, "model_key": model_ready, "docker": docker_ready,
                "sandbox": pool.status(), "active_runs": len(runs), "mode": "runtime"}

    @app.get("/api/models")
    async def models():
        return {"code": 200, "data": [{"id": "deepseek-chat", "name": "DeepSeek Chat",
            "provider": "deepseek", "supportsStream": True, "supportsThinking": False,
            "supportsTools": True, "supportsFileUpload": False,
            "enabled": bool(settings.deepseek_api_key.get_secret_value())}]}

    @app.get("/api/agents")
    async def agents():
        ready()
        listed = await store.list("agents", owner=owner_id())
        return {"code": 200, "data": [{**a, "modelId": a["model"]["model_id"],
            "systemPrompt": a["system_prompt"]} for a in listed]}

    @app.get("/api/tools")
    async def tools_catalog():
        """The tools an agent definition may select.

        Kept separate from the agent rows because the wizard has to offer the
        real catalogue: a hardcoded list in the browser drifts the moment a tool
        is added, and the create endpoint rejects the whole definition over one
        unknown name. Skill tools are left out on purpose — `prepare_agent`
        appends them to every agent, so they are not a choice.
        """
        ready()
        registry = tools_factory("catalog")
        return {"code": 200, "data": [
            {"name": tool.name, "description": tool.description,
             "permission": tool.permission, "source": tool.source}
            for name, tool in registry.tools.items() if name not in SKILL_TOOL_NAMES]}

    def public_file(record: dict) -> dict:
        """What the browser needs to list and fetch a file -- never its path."""
        return {"fileId": record["id"], "name": record["name"], "size": record["size"],
                "mimeType": record["mimeType"], "createdAt": record["createdAt"],
                "downloadPath": f"/api/files/{record['id']}/download"}

    async def session_or_new(identity: str) -> dict | None:
        """The session if it exists and is ours; None while it is still a draft.

        The row is created by the first run, but the reader uploads while
        composing that first message, so a missing row has to be allowed.
        Files are keyed by (owner, session) anyway, which is what actually stops
        a stranger from reaching them; an existing row that belongs to somebody
        else is the usual 404.
        """
        session_files.session_identity(identity)
        row = await store.db.sessions.find_one({"id": identity}, {"_id": 0})
        if row is not None and row.get("owner_id") != owner_id():
            raise HTTPException(404, "Session not found.")
        return row

    @app.post("/api/sessions/{session_id}/files", status_code=201)
    async def upload_session_file(session_id: str, file: UploadFile):
        """Take one file into the conversation's workspace.

        Guests are refused: an anonymous owner id is free to mint, and a disk
        that any visitor can fill is a denial of service, not a feature.
        """
        ready()
        uid = owner_id()
        if is_guest(uid):
            raise HTTPException(401, detail={"code": "sign_in_required",
                                             "reason": "Uploading a file needs an account."})
        await session_or_new(session_id)
        records = await store.list("session_files", {"sessionId": session_id}, owner=uid)
        if len(records) >= file_store.max_files:
            raise HTTPException(409, f"This conversation already holds {file_store.max_files} files. "
                                     "Delete one before uploading another.")
        used = sum(row.get("size", 0) for row in await store.list("session_files", {}, owner=uid))
        budget = min(file_store.max_bytes, file_store.max_owner_bytes - used)
        if budget <= 0:
            raise HTTPException(409, "Your upload allowance is used up. Delete a file to free space.")
        try:
            record = await file_store.save(session_id, file, budget, {row["label"] for row in records})
        except session_files.UploadRejected as exc:
            raise HTTPException(exc.status, str(exc)) from None
        stored = await store.put("session_files", record, owner=uid)
        logger.info("file uploaded session=%s owner=%s size=%d type=%s",
                    session_id, uid, stored["size"], stored["mimeType"])
        return {"code": 201, "data": public_file(stored)}

    @app.get("/api/sessions/{session_id}/files")
    async def list_session_files(session_id: str):
        ready()
        await session_or_new(session_id)
        records = await store.list("session_files", {"sessionId": session_id}, owner=owner_id())
        return {"code": 200, "data": [public_file(row) for row in
                                      sorted(records, key=lambda row: row.get("createdAt", ""))]}

    @app.delete("/api/sessions/{session_id}/files/{file_id}")
    async def delete_session_file(session_id: str, file_id: str):
        ready()
        uid = owner_id()
        await session_or_new(session_id)
        record = await store.get("session_files", file_id, owner=uid)
        if record is None or record.get("sessionId") != session_id:
            raise HTTPException(404, "File not found.")
        await store.db.session_files.delete_one({"id": file_id, "owner_id": uid})
        file_store.blob(session_id, file_id).unlink(missing_ok=True)
        return {"code": 200, "data": {"fileId": file_id}}

    async def owned_file(file_id: str):
        """The file this caller may read, after both locks, as (body, type, name).

        `body` is bytes for an artifact (stored in the row) or a `Path` for an
        uploaded blob (streamed from disk).
        """
        record = await store.get("artifacts", file_id, owner=owner_id())
        if record is None:
            # An uploaded file is fetched through the same URL, so the reader has
            # one way in regardless of who produced the bytes.
            uploaded = await store.get("session_files", file_id, owner=owner_id())
            if uploaded is None:
                raise HTTPException(404, "File not found.")
            # The row already came back inside this caller's ownership scope; the
            # session check is the second lock, and it has to tolerate a draft
            # conversation whose session row the first run has not created yet.
            await session_or_new(uploaded["sessionId"])
            path = file_store.blob(uploaded["sessionId"], uploaded["id"])
            if not path.is_file():
                raise HTTPException(404, "File not found.")
            return path, uploaded["mimeType"], uploaded["name"]
        # The artifact row was written by a run, so its session decides who may
        # read it: a stale row must not outlive the conversation's ownership.
        await owned_session(record.get("sessionId") or "")
        artifact = record["artifact"]
        return (bytes(record["content"]), artifact.get("mimeType") or "application/octet-stream",
                artifact["fileName"])

    def serve_file(body, mime: str, name: str, disposition: str):
        headers = {
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        }
        # Generated HTML is the one delivery the browser is allowed to render, and
        # only inside a sandbox; it is never inline, so no CSP is needed elsewhere.
        if mime == "text/html":
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
        if isinstance(body, bytes):
            return Response(body, media_type=mime, headers=headers)
        return FileResponse(body, media_type=mime, headers=headers)

    @app.get("/api/files/{file_id}/download")
    async def download_artifact(file_id: str):
        """Always an attachment: a file is never a document of ours."""
        ready()
        body, mime, name = await owned_file(file_id)
        return serve_file(body, mime, name, "attachment")

    @app.get("/api/files/{file_id}/view")
    async def view_artifact(file_id: str):
        """The one file the browser is allowed to render for itself: a PDF.

        Rendering it here meant shipping pdf.js and a worker, and pdf.js draws
        nothing for text set in a CJK font the document does not embed —
        ReportLab's STSong-Light is the common case — because mapping those glyph
        ids needs CMap tables we would also have to serve. The browser's own
        viewer substitutes a system font, so the same file renders. Everything
        else stays an attachment: an uploaded HTML file must never render on this
        origin, and a caller that asks for one gets a refusal rather than a
        silently different disposition.
        """
        ready()
        body, mime, name = await owned_file(file_id)
        if mime != "application/pdf":
            raise HTTPException(415, "Only a PDF can be shown in the viewer.")
        return serve_file(body, mime, name, "inline")

    @app.get("/api/skills")
    async def list_skills():
        ready()
        uid = owner_id()
        packages = await store.list("skills", owner=uid)
        return {"code": 200, "data": [{k: v for k, v in p.items() if k != "files"}
            | {"files": list(p["files"])} for p in packages]}

    @app.post("/api/skills", status_code=201)
    async def install_skill(package: SkillPackage):
        ready()
        owner_id()
        installed = await skill_manager.install(package)
        return {"code": 201, "data": {k: v for k, v in installed.items() if k != "files"}
            | {"files": list(installed["files"])}}

    @app.delete("/api/skills/{identity}")
    async def uninstall_skill(identity: str):
        ready()
        uid = owner_id()
        try:
            package = await skill_manager.delete(identity)
        except ValueError as exc:
            raise HTTPException(404 if "not installed" in str(exc) else 409, str(exc)) from None
        removed = 0
        # Only this account's workspaces. A sandbox workspace is named after the
        # container, not the session, so the session row is what decides
        # ownership; a skill id synced into someone else's conversation stays.
        roots = []
        for session in await store.list("sessions", owner=uid):
            lease = pool.workspaces.get(session["id"])
            roots.append(lease.root if lease is not None
                         else settings.agentforge_workspace / session["id"])
        for root in roots:
            try:
                removed += remove_synced_skill(root, identity)
            except (OSError, ValueError):
                logger.warning("Could not clean synced skill %s in %s", identity, root)
        return {"code": 200, "data": {"id": identity, "name": package["name"],
            "removed_workspaces": removed}}

    @app.get("/api/mcp/servers")
    async def list_mcp_servers():
        ready()
        owner_id()
        servers = await store.list("mcp_servers")
        return {"code": 200, "data": [public_server(server) for server in servers]}

    async def save_server(request: MCPServerRequest):
        """Discover once and store. The token stays in the database: nothing
        that leaves this endpoint contains it, and neither does anything printed."""
        record = request.model_dump(exclude_none=True, exclude={"type", "headers"})
        existing = await store.get("mcp_servers", request.id)
        if request.enabled is None:
            # A refresh (same URL, new token) must not resurrect a server the
            # operator switched off.
            record["enabled"] = (existing or {}).get("enabled", True)
        try:
            tools = await server_tools(record, settings.mcp_discover_timeout,
                                       settings.mcp_call_timeout)
        except Exception as exc:
            logger.warning("mcp discovery failed id=%s url=%s error=%s", request.id,
                           mask_url(request.url), describe(exc))
            if status_code(exc) in {401, 403}:
                raise HTTPException(422, "MCP server rejected the access token. For a hosted "
                    "service, supply the token from the same account that deployed it.") from None
            raise HTTPException(502, "MCP discovery failed. Check the URL and the Streamable "
                "HTTP transport.") from None
        server = await store.put("mcp_servers", {**record, "tools": [
            {"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]})
        logger.info("mcp server registered id=%s tools=%d token=%s", request.id,
                    len(tools), "yes" if record.get("token") else "no")
        return public_server(server)

    async def load_mcp_config(path):
        """Register whatever the startup file lists.

        Adding a server otherwise means a database row with a token in it, which
        is awkward to keep beside the code and impossible to review. An entry
        that is already registered is refreshed rather than duplicated, so a
        rotated token takes effect on restart; nothing is deleted for being
        absent from the file, because the file is a seed, not the only truth.
        Discovery costs a round trip per server, which is why this runs once at
        startup and in parallel.
        """
        if not path.is_file():
            return
        try:
            requests = parse_servers(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("mcp config ignored path=%s reason=%s", path, exc)
            return
        outcomes = await asyncio.gather(*(save_server(request) for request in requests),
                                        return_exceptions=True)
        for request, outcome in zip(requests, outcomes):
            if isinstance(outcome, BaseException):
                reason = getattr(outcome, "detail", None) or describe(outcome)
                logger.warning("mcp server %s from config not registered: %s", request.id, reason)
            else:
                logger.info("mcp server %s from config ready tools=%d", outcome["id"],
                            len(outcome.get("tools", [])))

    @app.post("/api/mcp/servers", status_code=201)
    async def register_mcp_server(request: MCPServerRequest):
        ready()
        owner_id()
        if await store.get("mcp_servers", request.id):
            raise HTTPException(409, "MCP server ID already exists.")
        return {"code": 201, "data": await save_server(request)}

    @app.post("/api/mcp/servers/import", status_code=201)
    async def import_mcp_server(payload: MCPConfig):
        """Accept the `mcpServers` block a catalogue gives you, verbatim."""
        ready()
        owner_id()
        try:
            request = parse_config(payload.config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # Re-pasting the same config with a new token refreshes that row: the
        # URL identifies the service, so a second import must not duplicate it.
        existing = next((server for server in await store.list("mcp_servers")
                         if server["url"] == request.url), None)
        if existing:
            request.id = existing["id"]
        else:
            base, suffix = request.id, 1
            while await store.get("mcp_servers", request.id):
                suffix += 1
                request.id = f"{base[:18].rstrip('_')}_{suffix}"
        return {"code": 201, "data": await save_server(request)}

    @app.delete("/api/mcp/servers/{identity}")
    async def remove_mcp_server(identity: str):
        ready()
        owner_id()
        if await store.get("mcp_servers", identity) is None:
            raise HTTPException(404, "MCP server is not registered.")
        if await store.list("agents", {"mcp_servers": identity}):
            raise HTTPException(409, "MCP server is referenced by a saved agent.")
        await store.db.mcp_servers.delete_one({"id": identity, **store.scope()})
        logger.info("mcp server removed id=%s", identity)
        return {"code": 200}

    @app.patch("/api/mcp/servers/{identity}")
    async def toggle_mcp_server(identity: str, payload: MCPToggle):
        """Switch a server off without forgetting its URL and token.

        Deleting is the wrong tool here: a hosted service that is temporarily
        broken would have to be registered again from scratch, token included.
        """
        ready()
        owner_id()
        server = await store.get("mcp_servers", identity)
        if server is None:
            raise HTTPException(404, "MCP server is not registered.")
        await store.put("mcp_servers", {**server, "enabled": payload.enabled})
        logger.info("mcp server id=%s enabled=%s", identity, payload.enabled)
        return {"code": 200, "data": public_server({**server, "enabled": payload.enabled})}

    async def prepare_agent(agent):
        agent = agent.model_copy(deep=True)
        agent.tools = list(dict.fromkeys([*agent.tools, *SKILL_TOOL_NAMES]))
        agent.system_prompt += "\n\nYou have a skill system. Use list_skills to discover installed " \
            "skills when the task may benefit from one; do not claim skills are unavailable without " \
            "checking. Call load_skill with the chosen catalog ID before using its scripts. Follow " \
            "the returned SKILL.md instructions and actual directory. Loading does not execute scripts; " \
            "use run_command inside the Docker sandbox; these commands run automatically unless " \
            "the agent explicitly requires approval. You can also install skills: use install_skill " \
            "with an explicit public GitHub source, a current sandbox folder, or inline files supplied " \
            "or explicitly requested by the user. Ask for the source when it is unspecified; never " \
            "invent a repository or replace a requested download with invented skill content. " \
            "Installation requires approval, imports files only, and returns the real installed ID. " \
            "GitHub downloads are performed by the backend install_skill tool, outside the sandbox; " \
            "an offline sandbox or restricted shell does not prevent GitHub skill installation. " \
            "Use list_skills to verify, then load_skill if the user wants to use it. To remove an " \
            "installed skill the user no longer wants, call delete_skill with its exact catalog ID; " \
            "it requires approval, clears the skill's synced sandbox files, and is refused while a " \
            "saved agent still selects the skill. " \
            "Do not run pip, " \
            "npm, git clone or curl to install skills; the offline sandbox cannot download packages. " \
            "Never invent tools, installation success, or script results."
        agent.system_prompt += "\n\nCreate requested deliverables using write_file, not just code in chat. " \
            "Use relative workspace paths. Ordinary non-hidden workspace file writes do not need " \
            "approval unless the agent policy explicitly requires it. Commands and installed skill " \
            "scripts inside the offline Docker sandbox also run automatically by default. Host shell " \
            "commands, skill installation and MCP calls still require approval. Do not ask the user " \
            "for routine sandbox confirmations; invoke tools and let the permission system decide. " \
            "For web deliverables, write an .html entry file and " \
            "its local CSS/JS files. The system delivers a downloadable HTML with local stylesheet and " \
            "script files inlined when the run ends. Prefer self-contained CSS (no imports or relative " \
            "asset URLs) and classic JS (no module imports/fetch). The in-app preview is script-free; " \
            "download and open the HTML to use scripts. Do not run shell commands merely to write " \
            "files or repeatedly read back files you just wrote. Never claim browser verification " \
            "unless a browser tool was actually used."
        if agent.skills and agent.sandbox_policy is None:
            raise HTTPException(422, "Skills require the Docker sandbox. Start a sandbox conversation.")
        if agent.sandbox_policy and agent.sandbox_policy.image not in PYTHON_SANDBOX_IMAGES:
            raise HTTPException(422, "Integrated file tools require the Python sandbox image.")
        try:
            instructions = await skill_manager.instructions(agent.skills)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if instructions:
            agent.system_prompt += "\n\nEnabled skills (scripts run only via sandbox commands; " \
                "dependencies are not installed automatically):\n" + instructions
        remote = []
        # Two ways a server joins a run. Pinning it on the agent is a promise:
        # if it is gone or broken the run fails loudly. Otherwise the registry
        # itself is the opt-in, so a server registered once is used by every run
        # without the conversation having to pick it - the product UI has no
        # per-run picker. Auto-attached servers that are merely offline are
        # skipped instead of failing the whole turn.
        selected, required = await run_servers(store, agent.mcp_servers,
                                               settings.mcp_auto_attach)
        attached = []
        for identity in selected:
            server = await store.get("mcp_servers", identity)
            if server is None:
                if identity in required:
                    raise HTTPException(422, f"MCP server is not registered: {identity}")
                continue
            attached.append((identity, server))
        # Discovered again on every run rather than read from the stored snapshot: a
        # hosted service can change its tools or expire. The calls are independent, so
        # they run together -- sequentially, four registered servers delayed the first
        # token by 11 s, which is the whole cost of a warm hosted endpoint.
        outcomes = await asyncio.gather(*[
            server_tools(server, settings.mcp_discover_timeout, settings.mcp_call_timeout)
            for _, server in attached], return_exceptions=True)
        for (identity, _), outcome in zip(attached, outcomes):
            strict = identity in required
            if isinstance(outcome, BaseException):
                logger.warning("mcp server %s unavailable at run time%s: %s", identity,
                               "" if strict else " (skipped)", describe(outcome))
                if not strict:
                    continue
                if status_code(outcome) in {401, 403}:
                    raise HTTPException(502, f"MCP server rejected the access token: {identity}") from None
                raise HTTPException(502, f"MCP server unavailable: {identity}") from None
            logger.info("mcp server %s contributed %d tools", identity, len(outcome))
            remote.extend(outcome)
        agent.tools = list(dict.fromkeys([*agent.tools, *(tool.name for tool in remote)]))
        if len(agent.tools) > 128:
            raise HTTPException(422, "Enable fewer MCP servers; at most 128 tools per run are supported.")
        return agent, remote

    async def validate_definition(agent: AgentDefinition) -> AgentDefinition:
        """Everything a stored definition must satisfy, checked the same way twice.

        Create and update share it: a definition that could not run is rejected at
        save time rather than at the first turn, when the failure would look like
        a runtime error to the person who just built it.
        """
        prepared, remote = await prepare_agent(agent)
        if agent.model.provider not in gateway.adapters:
            raise HTTPException(422, "Model provider is not registered.")
        try:
            # A fresh registry per call: the cached factory registry accumulates
            # remote tools across requests, so registering this agent's MCP tools
            # into it raises "Duplicate tool" the second time the same server is
            # used. Built-in names are copied in, nothing else is inherited.
            validation = ToolRegistry()
            for tool in tools_factory("validation").tools.values():
                if tool.source == "builtin":
                    validation.register(tool)
            for tool in remote:
                if tool.name not in validation.tools:
                    validation.register(tool)
            validation.schemas(prepared.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return prepared

    @app.post("/api/agents", status_code=201)
    async def create_agent(agent: AgentDefinition):
        ready()
        uid = owner_id()
        if agent.id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent id is reserved.")
        await validate_definition(agent)
        if await store.get("agents", agent.id, owner=uid):
            raise HTTPException(409, "Agent ID already exists.")
        return {"code": 201, "data": await store.put("agents", agent.model_dump(), owner=uid)}

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, agent: AgentDefinition):
        ready()
        uid = owner_id()
        # The built-in row is shared read-only: every account runs it, so no
        # account may rewrite what the others see.
        if agent_id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent is shared and cannot be edited.")
        if await store.get("agents", agent_id, owner=uid) is None:
            raise HTTPException(404, "Agent not found.")
        # The path decides the identity: a body carrying another id must not be
        # able to rename this row over somebody else's (or its own previous id).
        agent = agent.model_copy(update={"id": agent_id})
        await validate_definition(agent)
        return {"code": 200, "data": await store.put("agents", agent.model_dump(), owner=uid)}

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str):
        ready()
        if agent_id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent is shared and cannot be deleted.")
        result = await store.db.agents.delete_one({"id": agent_id, "owner_id": owner_id()})
        if not result.deleted_count:
            raise HTTPException(404, "Agent not found.")
        return {"code": 200, "data": {"id": agent_id}}

    async def execute(agent, task, session_id, bus, turn_id, remote, resume=False, uploads=()):
        current_session.set(session_id)
        # The run lock recorded who owns this session when it was started. The
        # sandbox lease is bound to the same account, so a task created without
        # the request's context cannot quietly inherit someone else's container.
        lease_owner = session_owners.get(session_id, "") or owner_id()
        try:
            async with asyncio.timeout(900):
                previous = await store.get("sessions", session_id)
                isolated = agent.sandbox_policy is not None
                if previous and previous.get("isolated", False) != isolated:
                    raise ValueError("Start a new conversation to change execution environment.")
                if isolated:
                    for identity in (previous or {}).get("loaded_skills", []):
                        if identity not in agent.skills and len(agent.skills) < 8 and await store.get("skills", identity):
                            agent.skills.append(identity)
                registry = tools_factory(session_id)
                async def skill_loaded(package):
                    await bus.emit({"type": "skill_loaded", "sessionId": session_id, "turnId": turn_id,
                        "skill_id": package["id"], "name": package["name"]})
                async def skill_installed(package):
                    await bus.emit({"type": "skill_installed", "sessionId": session_id, "turnId": turn_id,
                        "skill_id": package["id"], "name": package["name"], "sha256": package["sha256"]})
                async def skill_deleted(package):
                    await bus.emit({"type": "skill_deleted", "sessionId": session_id, "turnId": turn_id,
                        "skill_id": package["id"], "name": package["name"]})
                if isolated:
                    lease = await pool.acquire(session_id, agent.sandbox_policy.image,
                                               owner=lease_owner)
                    run_sandboxes[session_id] = lease.sandbox
                    registry = ToolRegistry()
                    for name in agent.tools:
                        # Asking a human needs no container, so it stays a builtin
                        # even when every other tool is redirected into the sandbox.
                        if name == AskUserTool.name:
                            registry.register(AskUserTool())
                            continue
                        # The web tools reach the internet from the harness, not
                        # from the container: the sandbox has no route out and is
                        # not getting one, so redirecting these into it would only
                        # produce a DNS failure dressed up as a tool result.
                        if name in WEB_TOOL_MAP:
                            registry.register(WEB_TOOL_MAP[name]())
                            continue
                        # Same reasoning for the catalogue: it is one GET from the
                        # harness to a fixed host with a token the container must
                        # never see.
                        if name in ECOM_TOOL_MAP:
                            registry.register(ECOM_TOOL_MAP[name](settings))
                            continue
                        if name not in {tool.name for tool in remote} | set(SKILL_TOOL_NAMES):
                            registry.register(SandboxTool(pool.runner, lease.sandbox, name,
                                                         agent.sandbox_policy.timeout))
                for name in SKILL_TOOL_NAMES:
                    registry.tools.pop(name, None)
                    registry.register(SkillTool(skill_manager, name, pool.runner,
                        run_sandboxes.get(session_id), agent, skill_loaded, skill_installed,
                        skill_deleted))
                for tool in remote:
                    registry.register(tool)
                isolated_tools[session_id] = registry
                await store.put("sessions", {**(previous or {}), "id": session_id,
                    "sessionId": session_id, "isolated": isolated})
                # The reader's files are copied into whichever workspace this run
                # actually uses -- host directory or sandbox -- because the two
                # trees are different and the model only sees the active one. The
                # copy is per turn, so a sandbox is available without teaching the
                # upload endpoint how a container is created.
                if uploads:
                    sandbox = run_sandboxes.get(session_id)
                    root = (sandbox.workspace.root if sandbox
                            else settings.agentforge_workspace / session_files.session_identity(session_id))
                    manifest = await asyncio.to_thread(file_store.sync_into, list(uploads), root)
                    agent = agent.model_copy(update={
                        "system_prompt": agent.system_prompt + session_files.manifest_prompt(manifest)})
                await runtime.run(agent, task, session_id, bus, turn_id=turn_id, resume=resume)
        except Exception:
            # exc_info matters here: the run is a background task, so without it a
            # failure is invisible and the user only sees a generic error event.
            logger.warning("Run failed for session %s", session_id, exc_info=True)
            if not bus.events or bus.events[-1]["type"] != "error":
                await bus.emit({"type": "error", "sessionId": session_id,
                                "message": "Run failed. Check configuration and retry."})
        finally:
            try:
                if session_id in isolated_tools:
                    current = await store.get("sessions", session_id)
                    if current:
                        await store.put("sessions", {**current, "loaded_skills": agent.skills})
            except Exception:
                logger.warning("Could not persist loaded skills for session %s", session_id)
            finally:
                isolated_tools.pop(session_id, None)
                run_sandboxes.pop(session_id, None)
                await pool.release(session_id)

    def start(agent, task, session_id, turn_id, remote=(), resume=False, uploads=()):
        if session_id in runs and not runs[session_id][1].done():
            raise HTTPException(409, "Session already has an active run.")
        session_owners[session_id] = owner_id()
        def workspace():
            sandbox = run_sandboxes.get(session_id)
            return sandbox.workspace if sandbox else Workspace(
                settings.agentforge_workspace / session_files.session_identity(session_id))
        bus = ArtifactEventBus(runtime, store, workspace)
        running = asyncio.create_task(
            execute(agent, task, session_id, bus, turn_id, remote, resume, uploads))
        running.add_done_callback(lambda task: retire_run(session_id, task, bus))
        runs[session_id] = (bus, running)
        return bus, running

    @app.post("/api/agents/run")
    async def run(request: LegacyRequest):
        ready(model=True)
        uid = owner_id()
        if not request.input.strip() or request.sessionId == "default":
            raise HTTPException(422, "Use nonempty input and a unique session ID.")
        session_files.session_identity(request.sessionId)
        # Resuming a run does not buy a new turn: it continues the one already paid for.
        if not request.resume:
            await bill_guest_turn()
        # The browser supplies sessionId, so an id that already exists has to be
        # proven to be ours before the run may continue it.
        row = await store.db.sessions.find_one({"id": request.sessionId}, {"_id": 0})
        if row is not None and row.get("owner_id") != uid:
            raise HTTPException(404, "Session not found.")
        # A file id is only usable inside the conversation it was uploaded to and
        # by the account that uploaded it; anything else is the same 404.
        uploads = []
        for file_id in request.fileIds:
            record = await store.get("session_files", file_id, owner=uid)
            if record is None or record.get("sessionId") != request.sessionId:
                raise HTTPException(404, "File not found.")
            uploads.append(record)
        definition = await store.get(
            "agents", request.agentIds[0] if request.agentIds else "default", owner=uid)
        if not definition:
            raise HTTPException(404, "Agent not found.")
        agent = AgentDefinition.model_validate(definition)
        if not request.agentIds:
            agent.model = ModelRef(model_id=request.model)
            agent.sandbox_policy = SandboxPolicy() if request.sandbox else None
            agent.skills, agent.mcp_servers = request.skills, request.mcp_servers
        agent, remote = await prepare_agent(agent)
        bus, running = start(agent, request.input, request.sessionId, request.turnId, remote,
                             request.resume, uploads)
        if not request.stream:
            await running
            final = bus.events[-1] if bus.events else None
            if final is None:
                raise HTTPException(502, "Run produced no result.")
            if final["type"] == "error":
                raise HTTPException(502, final["message"])
            return {"code": 200, "data": legacy_result(final["result"], request.input)}
        async def frames():
            try:
                async for frame in event_frames(bus, request.input, legacy=True):
                    yield frame
            finally:
                terminal = any(event["type"] in {"done", "error"} for event in bus.events)
                if not running.done() and not terminal:
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        return StreamingResponse(frames(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: SessionRequest):
        ready(model=True)
        uid = owner_id()
        definition = await store.get("agents", request.agent_id, owner=uid)
        if not definition:
            raise HTTPException(404, "Agent not found.")
        await bill_guest_turn()
        session_id, turn_id = new_id(), new_id()
        agent, remote = await prepare_agent(AgentDefinition.model_validate(definition))
        bus, _ = start(agent, request.task, session_id, turn_id, remote)
        async with bus.condition:
            # Bound the wait: a provider that accepts the request but never emits
            # must not hold this HTTP request open for the full 900s run timeout.
            try:
                async with asyncio.timeout(30):
                    await bus.condition.wait_for(lambda: bool(bus.events))
            except TimeoutError:
                logger.warning("Session %s emitted no first event within 30s.", session_id)
        return {"session_id": session_id, "turn_id": turn_id,
                "trace_id": bus.events[0].get("trace_id") if bus.events else None}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, after: int = Query(0, ge=0)):
        uid = owner_id()
        recorded = session_owners.get(session_id)
        if recorded is not None and recorded != uid:
            raise HTTPException(404, "Session events are unavailable in this process.")
        found = lookup_run(session_id)
        if found is None:
            # A restart forgets the in-memory bus; the stored row still decides
            # who may ask for the replay.
            await owned_session(session_id)
            raise HTTPException(404, "Session events are unavailable in this process.")
        bus, terminal = found
        if after >= len(bus.events) and terminal:
            return StreamingResponse(iter([]), media_type="text/event-stream")
        return StreamingResponse(event_frames(bus, after=after), media_type="text/event-stream")

    def run_state(session_id):
        entry = runs.get(session_id)
        if entry is None:
            if session_id in finished_runs:
                return None, True
            return None, None
        bus, running = entry
        return bus, running.done()

    @app.get("/api/sessions/{session_id}/context")
    async def session_context(session_id: str):
        # The gauge reads this after a reload: the run's context events are gone, but
        # the last measurement was committed with the transcript it describes.
        ready()
        session = await store.get("sessions", session_id, owner=owner_id())
        if session is None:
            raise HTTPException(404, "Session not found.")
        return {"code": 200, "data": session.get("context")}

    @app.get("/api/sessions")
    async def list_sessions():
        ready()
        sessions = await store.list("sessions", owner=owner_id())
        view = []
        for session in sessions:
            session_id = session.get("id") or session.get("sessionId")
            _, terminal = run_state(session_id)
            view.append({"id": session_id, "agent_id": session.get("agent_id"),
                "status": session.get("status") or ("running" if terminal is False else "idle"),
                "isolated": bool(session.get("isolated", False)),
                "active": terminal is False,
                "updated_at": session.get("updated_at")})
        view.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {"code": 200, "data": view}

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        uid = owner_id()
        recorded = session_owners.get(session_id)
        if recorded is not None and recorded != uid:
            raise HTTPException(404, "No active run for this session.")
        entry = runs.get(session_id)
        if entry is None:
            raise HTTPException(404, "No active run for this session.")
        bus, running = entry
        # Release approvals first so the loop unwinds through its normal path and
        # the trace records a denial instead of an abrupt cancellation.
        cancelled = approvals.cancel_session(session_id)
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        return {"code": 200, "data": {"session_id": session_id, "stopped": True,
            "cancelled_approvals": cancelled}}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        ready()
        await owned_session(session_id)
        approvals.forget_session(session_id)
        entry = runs.get(session_id)
        if entry is not None:
            bus, running = entry
            approvals.cancel_session(session_id)
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        # Drop the session together with everything derived from it, so removing a
        # conversation does not leave orphaned traces, turns or artifacts behind.
        traces = await store.list("traces", {"sessionId": session_id})
        for trace in traces:
            for collection in ("turns", "tool_calls"):
                await store.db[collection].delete_many({"trace_id": trace["id"]})
        await store.db.traces.delete_many({"sessionId": session_id})
        await store.db.checkpoints.delete_many({"sessionId": session_id})
        await store.db.artifacts.delete_many({"sessionId": session_id})
        # Uploaded blobs are the reader's data, so they go with the conversation
        # rather than lingering in a store nothing lists any more.
        await store.db.session_files.delete_many({"sessionId": session_id, **store.scope()})
        await asyncio.to_thread(file_store.discard, session_id)
        result = await store.db.sessions.delete_one({"id": session_id, **store.scope()})
        if result.deleted_count == 0:
            raise HTTPException(404, "Session not found.")
        finished_runs.pop(session_id, None)
        # The conversation is gone, so nothing it produced may stay behind: the
        # sandbox would otherwise keep running with the files it wrote, and the
        # workspace directory is protected from every sweep while the pool
        # remembers it.
        await pool.discard(session_id, owner=owner_id())
        isolated_tools.pop(session_id, None)
        run_sandboxes.pop(session_id, None)
        session_owners.pop(session_id, None)
        return {"code": 200, "data": {"id": session_id, "deleted": True}}

    @app.get("/api/approvals")
    async def list_approvals():
        # Decisions resolve in-memory, so this stays available even when Mongo is down.
        uid = owner_id()
        visible = []
        for item in approvals.pending_list():
            session_id = item.get("sessionId") or ""
            if not session_id:
                continue
            owner = session_owners.get(session_id)
            if owner is None:
                owner = uid if await store.get("sessions", session_id, owner=uid) else None
            if owner == uid:
                visible.append(item)
        return {"code": 200, "data": visible}

    @app.get("/api/interventions/policy")
    async def intervention_policy():
        # Answers "what does the system stop me for" without reading the source.
        return {"code": 200, "data": policy.explain()}

    @app.post("/api/sandbox/exec")
    async def sandbox_exec(request: SandboxExecRequest):
        # Runs host-supplied files and a command inside a disposable offline
        # container. This is the escape hatch that keeps skill scripts and other
        # untrusted code off the host: the same pool, seccomp profile and network
        # isolation the agent uses, without going through a model turn.
        session_id = new_id()
        lease = None
        try:
            lease = await pool.acquire(session_id, request.image, owner=owner_id())
            for item in request.files:
                relative = item.path
                content = item.content
                if relative.startswith("/") or ".." in relative.split("/"):
                    raise HTTPException(422, f"Unsafe file path: {relative}")
                try:
                    target = lease.sandbox.workspace.path(relative)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_text, content, encoding="utf-8")
            result = await pool.runner.execute(lease.sandbox, request.command, request.timeout)
            return {"code": 200, "data": {"ok": result.ok, "output": result.output, "error": result.error}}
        except SandboxUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        finally:
            if lease is not None:
                await pool.release(session_id)

    @app.post("/api/approvals/{call_id}")
    async def approve(call_id: str, request: ApprovalRequest):
        uid = owner_id()
        session_id = approvals.owners.get(call_id)
        if session_id is None:
            # Resolved already, or never existed. Both answer the same way so a
            # caller cannot use this endpoint to probe for approval ids.
            raise HTTPException(409, "Approval is no longer pending.")
        recorded = session_owners.get(session_id)
        if recorded is not None and recorded != uid:
            raise HTTPException(404, "Approval not found.")
        if recorded is None and await store.get("sessions", session_id, owner=uid) is None:
            raise HTTPException(404, "Approval not found.")
        try:
            approvals.resolve(call_id, request.approved, request.reason, request.remember)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": request.approved, "remember": request.remember}

    @app.get("/api/traces/{session_id}")
    async def traces(session_id: str):
        ready()
        await owned_session(session_id)
        results = await store.list("traces", {"sessionId": session_id})
        for trace in results:
            trace["turns"] = sorted(await store.list("turns", {"trace_id": trace["id"]}), key=lambda t: t["index"])
            trace["tool_calls"] = await store.list("tool_calls", {"trace_id": trace["id"]})
        return {"sessionId": session_id, "traces": results}

    @app.post("/api/chat/completions")
    async def chat(request: CompletionRequest):
        adapter = gateway.adapters.get("deepseek")
        if isinstance(adapter, DeepSeekAdapter) and not adapter.api_key:
            raise HTTPException(503, "Configure the DeepSeek API key in the Harness environment and restart.")
        if not request.stream:
            raise HTTPException(422, "Use stream=true.")
        await bill_guest_turn()
        async def frames():
            try:
                user_request = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
                messages = with_response_language([m.model_dump() for m in request.messages], user_request)
                async for chunk in gateway.chat(messages, [], ModelRef(model_id=request.model)):
                    if chunk.type == "content":
                        payload = {"choices": [{"delta": {"content": chunk.text}}]}
                    elif chunk.type == "usage":
                        payload = {"choices": [], "usage": {"prompt_tokens": chunk.tokens_in,
                            "completion_tokens": chunk.tokens_out, "total_tokens": chunk.tokens_in + chunk.tokens_out}}
                    else:
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
            except ModelOutputError as exc:
                chinese = any("\u3400" <= c <= "\u9fff" for c in user_request)
                message = ("模型输出达到长度限制，回答尚未完成。请缩小任务范围或要求分段输出。" if chinese
                           else "Model output reached its length limit. Request a smaller response or split the task.")
                yield f"data: {json.dumps({'error': {'message': message, 'code': exc.code}})}\n\n"
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                message = {
                    401: "Model provider rejected the API key. Check DEEPSEEK_API_KEY and restart.",
                    402: "Model provider balance is insufficient. Check your provider account.",
                    429: "Model provider rate limit reached. Please retry later.",
                }.get(status, f"Model provider request failed (HTTP {status}). Please retry later.")
                yield f"data: {json.dumps({'error': {'message': message}})}\n\n"
                return
            except httpx.TransportError:
                yield f"data: {json.dumps({'error': {'message': 'Model connection interrupted. Please retry.'}})}\n\n"
                return
            yield "data: [DONE]\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream")

    return app
