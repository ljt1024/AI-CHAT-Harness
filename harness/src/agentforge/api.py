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
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from agentforge.config import Settings
from agentforge.artifacts import ArtifactEventBus
from agentforge.capabilities import (MCPServerRequest, MAX_CONFIG, parse_config,
                                     public_server, run_servers, server_tools)
from agentforge.context import ContextManager
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.base import ModelOutputError
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime
from agentforge.logging_config import configure, request_id
from agentforge.redact import describe, mask_url, status_code
from agentforge.middleware import MiddlewareStack
from agentforge.models import AgentDefinition, ModelRef, new_id
from agentforge.permissions import ApprovalManager
from agentforge.policy import InterventionPolicy
from agentforge.prompts import with_response_language
from agentforge.store import MongoDBStore
from agentforge.tools.ask import AskUserTool
from agentforge.tools.base import ToolRegistry
from agentforge.tools.fs import FileTool, Workspace
from agentforge.tools.shell import ShellTool
from agentforge.sandbox.docker_runner import DockerRunner, SandboxUnavailable
from agentforge.sandbox.pool import SandboxPool
from agentforge.sandbox.tools import SandboxTool
from agentforge.models import SandboxPolicy
from agentforge.skills import (SKILL_TOOL_NAMES, SkillManager, SkillPackage, SkillTool,
    remove_synced_skill)

logger = logging.getLogger(__name__)


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
    gateway = gateway or ModelGateway()
    if "deepseek" not in gateway.adapters:
        gateway.register("deepseek", DeepSeekAdapter(settings.deepseek_api_key.get_secret_value()))
    pool = SandboxPool(DockerRunner(settings.agentforge_workspace / "sandboxes"),
        warm_size=settings.sandbox_warm_size, max_size=settings.sandbox_max_size,
        idle_seconds=settings.sandbox_idle_seconds)
    isolated_tools = {}
    run_sandboxes = {}
    skill_manager = SkillManager(store)

    def tools_factory(session_id):
        if session_id in isolated_tools:
            return isolated_tools[session_id]
        workspace = Workspace(settings.agentforge_workspace / session_id)
        registry = ToolRegistry()
        for name in ("read_file", "write_file", "grep", "glob"):
            registry.register(FileTool(workspace, name))
        registry.register(ShellTool(workspace))
        registry.register(AskUserTool())
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
            finished_runs.popitem(last=False)

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
        tools=["read_file", "write_file", "grep", "glob", "run_command", "ask_user"])

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        try:
            await store.setup()
            await pool.start()
            if await store.get("agents", "default") is None:
                await store.put("agents", default.model_dump())
            app.state.ready = True
        except Exception:
            logger.warning("MongoDB unavailable; data endpoints return 503.")
        yield
        for _, task in runs.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in runs.values()), return_exceptions=True)
        await pool.close()

    app = FastAPI(title="AgentForge", lifespan=lifespan)
    app.state.runtime, app.state.approvals = runtime, approvals
    app.state.sandbox_pool = pool
    # Exposed so operators (and the runtime status endpoint) can inspect what is running.
    app.state.runs, app.state.finished_runs = runs, finished_runs

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
        return {"code": 200, "data": [{**a, "modelId": a["model"]["model_id"],
            "systemPrompt": a["system_prompt"]} for a in await store.list("agents")]}

    @app.get("/api/files/{file_id}/download")
    async def download_artifact(file_id: str):
        ready()
        record = await store.get("artifacts", file_id)
        if record is None:
            raise HTTPException(404, "File not found.")
        artifact = record["artifact"]
        return Response(bytes(record["content"]), media_type="text/html", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact['fileName'], safe='')}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
        })

    @app.get("/api/skills")
    async def list_skills():
        ready()
        return {"code": 200, "data": [{k: v for k, v in p.items() if k != "files"}
            | {"files": list(p["files"])} for p in await store.list("skills")]}

    @app.post("/api/skills", status_code=201)
    async def install_skill(package: SkillPackage):
        ready()
        installed = await skill_manager.install(package)
        return {"code": 201, "data": {k: v for k, v in installed.items() if k != "files"}
            | {"files": list(installed["files"])}}

    @app.delete("/api/skills/{identity}")
    async def uninstall_skill(identity: str):
        ready()
        try:
            package = await skill_manager.delete(identity)
        except ValueError as exc:
            raise HTTPException(404 if "not installed" in str(exc) else 409, str(exc)) from None
        removed = 0
        for directory in settings.agentforge_workspace.glob("**/.skills"):
            try:
                removed += remove_synced_skill(directory.parent, identity)
            except (OSError, ValueError):
                logger.warning("Could not clean synced skill %s in %s", identity, directory)
        return {"code": 200, "data": {"id": identity, "name": package["name"],
            "removed_workspaces": removed}}

    @app.get("/api/mcp/servers")
    async def list_mcp_servers():
        ready()
        servers = await store.list("mcp_servers")
        return {"code": 200, "data": [public_server(server) for server in servers]}

    async def save_server(request: MCPServerRequest):
        """Discover once and store. The token stays in the database: nothing
        that leaves this endpoint contains it, and neither does anything printed."""
        record = request.model_dump(exclude_none=True, exclude={"type", "headers"})
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

    @app.post("/api/mcp/servers", status_code=201)
    async def register_mcp_server(request: MCPServerRequest):
        ready()
        if await store.get("mcp_servers", request.id):
            raise HTTPException(409, "MCP server ID already exists.")
        return {"code": 201, "data": await save_server(request)}

    @app.post("/api/mcp/servers/import", status_code=201)
    async def import_mcp_server(payload: MCPConfig):
        """Accept the `mcpServers` block a catalogue gives you, verbatim."""
        ready()
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
        if await store.list("agents", {"mcp_servers": identity}):
            raise HTTPException(409, "MCP server is referenced by a saved agent.")
        await store.db.mcp_servers.delete_one({"id": identity})
        logger.info("mcp server removed id=%s", identity)
        return {"code": 200}

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
        if agent.sandbox_policy and agent.sandbox_policy.image != "python:3.12-slim":
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
        for identity in selected:
            strict = identity in required
            server = await store.get("mcp_servers", identity)
            if server is None:
                if not strict:
                    continue
                raise HTTPException(422, f"MCP server is not registered: {identity}")
            try:
                # Discovered again on every run rather than read from the stored
                # snapshot: a hosted service can change its tools or expire.
                discovered = await server_tools(server, settings.mcp_discover_timeout,
                                                settings.mcp_call_timeout)
            except Exception as exc:
                logger.warning("mcp server %s unavailable at run time%s: %s", identity,
                               "" if strict else " (skipped)", describe(exc))
                if not strict:
                    continue
                if status_code(exc) in {401, 403}:
                    raise HTTPException(502, f"MCP server rejected the access token: {identity}") from None
                raise HTTPException(502, f"MCP server unavailable: {identity}") from None
            logger.info("mcp server %s contributed %d tools", identity, len(discovered))
            remote.extend(discovered)
        agent.tools = list(dict.fromkeys([*agent.tools, *(tool.name for tool in remote)]))
        if len(agent.tools) > 128:
            raise HTTPException(422, "Enable fewer MCP servers; at most 128 tools per run are supported.")
        return agent, remote

    @app.post("/api/agents", status_code=201)
    async def create_agent(agent: AgentDefinition):
        ready()
        prepared, remote = await prepare_agent(agent)
        if agent.model.provider not in gateway.adapters:
            raise HTTPException(422, "Model provider is not registered.")
        try:
            validation = tools_factory("validation")
            for tool in remote:
                validation.register(tool)
            validation.schemas(prepared.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if await store.get("agents", agent.id):
            raise HTTPException(409, "Agent ID already exists.")
        return {"code": 201, "data": await store.put("agents", agent.model_dump())}

    async def execute(agent, task, session_id, bus, turn_id, remote, resume=False):
        current_session.set(session_id)
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
                    lease = await pool.acquire(session_id, agent.sandbox_policy.image)
                    run_sandboxes[session_id] = lease.sandbox
                    registry = ToolRegistry()
                    for name in agent.tools:
                        # Asking a human needs no container, so it stays a builtin
                        # even when every other tool is redirected into the sandbox.
                        if name == AskUserTool.name:
                            registry.register(AskUserTool())
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

    def start(agent, task, session_id, turn_id, remote=(), resume=False):
        if session_id in runs and not runs[session_id][1].done():
            raise HTTPException(409, "Session already has an active run.")
        def workspace():
            sandbox = run_sandboxes.get(session_id)
            return sandbox.workspace if sandbox else Workspace(settings.agentforge_workspace / session_id)
        bus = ArtifactEventBus(runtime, store, workspace)
        running = asyncio.create_task(execute(agent, task, session_id, bus, turn_id, remote, resume))
        running.add_done_callback(lambda task: retire_run(session_id, task, bus))
        runs[session_id] = (bus, running)
        return bus, running

    @app.post("/api/agents/run")
    async def run(request: LegacyRequest):
        ready(model=True)
        if not request.input.strip() or request.sessionId == "default":
            raise HTTPException(422, "Use nonempty input and a unique session ID.")
        if request.fileIds:
            raise HTTPException(422, "File uploads require Tier 2.")
        definition = await store.get("agents", request.agentIds[0] if request.agentIds else "default")
        if not definition:
            raise HTTPException(404, "Agent not found.")
        agent = AgentDefinition.model_validate(definition)
        if not request.agentIds:
            agent.model = ModelRef(model_id=request.model)
            agent.sandbox_policy = SandboxPolicy() if request.sandbox else None
            agent.skills, agent.mcp_servers = request.skills, request.mcp_servers
        agent, remote = await prepare_agent(agent)
        bus, running = start(agent, request.input, request.sessionId, request.turnId, remote,
                             request.resume)
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
        definition = await store.get("agents", request.agent_id)
        if not definition:
            raise HTTPException(404, "Agent not found.")
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
        found = lookup_run(session_id)
        if found is None:
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
        session = await store.get("sessions", session_id)
        if session is None:
            raise HTTPException(404, "Session not found.")
        return {"code": 200, "data": session.get("context")}

    @app.get("/api/sessions")
    async def list_sessions():
        ready()
        sessions = await store.list("sessions")
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
        result = await store.db.sessions.delete_one({"id": session_id})
        if result.deleted_count == 0:
            raise HTTPException(404, "Session not found.")
        finished_runs.pop(session_id, None)
        return {"code": 200, "data": {"id": session_id, "deleted": True}}

    @app.get("/api/approvals")
    async def list_approvals():
        # Decisions resolve in-memory, so this stays available even when Mongo is down.
        return {"code": 200, "data": approvals.pending_list()}

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
            lease = await pool.acquire(session_id, request.image)
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
        try:
            approvals.resolve(call_id, request.approved, request.reason, request.remember)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": request.approved, "remember": request.remember}

    @app.get("/api/traces/{session_id}")
    async def traces(session_id: str):
        ready()
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
