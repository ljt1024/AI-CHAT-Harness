"""What every handler needs, assembled once.

Before this module existed the wiring was a closure: `create_app` opened with
`settings/store/gateway/pool/auth/...` as local names and 75 nested functions
read them directly. That is why the file could not be split -- a route moved to
another module has nothing to close over. The state and the helpers that act on
it are now one object, so a router can be handed `deps` and keep its body.

Nothing here decides *what* the app is; `app.create_app` still owns construction
and `lifespan` owns startup. This is the shared vocabulary between them.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, Response

from agentforge import email_address, netguard, secrets_box, session_files
from agentforge import phone as phone_number
from agentforge.artifacts import ArtifactEventBus
from agentforge.auth import (GUEST_COOKIE, SESSION_COOKIE, AuthService, current_user,
                             is_guest, valid_guest_id)
from agentforge.capabilities import (MCPServerRequest, parse_servers, public_server,
                                     run_servers, server_tools)
from agentforge.catalog import parse_catalog
from agentforge.config import Settings
from agentforge.context import ContextManager
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime
from agentforge.middleware import MiddlewareStack
from agentforge.models import PYTHON_SANDBOX_IMAGES, AgentDefinition
from agentforge.permissions import ApprovalManager
from agentforge.policy import InterventionPolicy
from agentforge.redact import describe, mask_url, status_code
from agentforge.sandbox.pool import SandboxPool
from agentforge.sandbox.tools import SandboxTool
from agentforge.skills import SKILL_TOOL_NAMES, SkillManager, SkillTool
from agentforge.store import MongoDBStore
from agentforge.tools.ask import AskUserTool
from agentforge.tools.base import ToolRegistry
from agentforge.tools.ecommerce import ECOM_TOOL_MAP, ECOM_TOOLS
from agentforge.tools.fs import FileTool, Workspace
from agentforge.tools.shell import ShellTool
from agentforge.tools.web import WEB_TOOL_MAP, WEB_TOOLS

logger = logging.getLogger(__name__)


@dataclass
class AppDeps:
    """The request-scoped state and the services built on it."""

    settings: Settings
    store: MongoDBStore
    gateway: ModelGateway
    pool: SandboxPool
    auth: AuthService
    sms: Any
    mail: Any
    file_store: session_files.SessionFiles
    skill_manager: SkillManager
    runtime: Runtime
    approvals: ApprovalManager
    policy: InterventionPolicy
    # Session id -> (bus, running task). In memory on purpose: a run belongs to
    # the process that started it.
    runs: dict
    # Finished runs keep their event log briefly so a reconnecting SSE client can
    # replay the tail it missed. Bounded, otherwise long-lived processes would
    # retain every event log forever.
    finished_runs: OrderedDict
    # Session id -> account that started it. Runs and finished-run buses live in
    # memory, so this is what lets the SSE, stop and delete endpoints tell the
    # caller's session from a stranger's without a database round trip.
    session_owners: dict
    isolated_tools: dict
    run_sandboxes: dict
    # The runtime only passes (agent, tool, call, emit), so carry the session on a
    # context var to associate a pending approval with the run that requested it.
    current_session: contextvars.ContextVar
    # FastAPI app, for the two places that read `app.state` (readiness, and the
    # attributes tests set to simulate an outage).
    app: Any = None

    # Built here rather than passed in: the runtime needs this object's tool
    # factory, so constructing it before `deps` exists would be circular.
    runtime: Runtime = field(init=False)

    FINISHED_RUN_LIMIT = 16

    # --- wiring -----------------------------------------------------------

    def __post_init__(self) -> None:
        """Build the runtime and hand it the two callbacks only this object answers."""
        self.runtime = Runtime(self.gateway, self.store, self.tools_factory)
        self.runtime.middleware = MiddlewareStack()
        self.runtime.context_manager = ContextManager()
        layer = next(layer for layer in self.runtime.middleware.layers
                     if layer.name == "SkillsSync")
        layer.sync, layer.enabled = self.sync_skills, True
        self.runtime.permission_check = self.permission_check

    def tools_factory(self, session_id):
        if session_id in self.isolated_tools:
            return self.isolated_tools[session_id]
        # The session id becomes a directory name here, so it is validated at
        # the door rather than assumed to be the uuid the browser normally sends.
        workspace = Workspace(self.settings.agentforge_workspace
                              / session_files.session_identity(session_id))
        registry = ToolRegistry()
        for name in ("read_file", "write_file", "grep", "glob"):
            registry.register(FileTool(workspace, name))
        registry.register(ShellTool(workspace))
        registry.register(AskUserTool())
        for tool in WEB_TOOLS:
            registry.register(tool())
        for tool in ECOM_TOOLS:
            registry.register(tool(self.settings))
        for name in SKILL_TOOL_NAMES:
            registry.register(SkillTool(self.skill_manager, name))
        return registry

    async def sync_skills(self, ctx):
        if ctx.agent.skills:
            sandbox = self.run_sandboxes[ctx.metadata["session_id"]]
            ctx.metadata["skills"] = await self.skill_manager.sync(ctx.agent.skills, sandbox)

    async def permission_check(self, agent, tool, call, emit):
        return await self.approvals.check(agent, tool, call, emit,
                                          session_id=self.current_session.get())

    # --- runs -------------------------------------------------------------

    def retire_run(self, session_id, task, bus):
        if self.runs.get(session_id, (None, None))[1] is not task:
            return
        self.runs.pop(session_id, None)
        self.finished_runs[session_id] = bus
        self.finished_runs.move_to_end(session_id)
        while len(self.finished_runs) > self.FINISHED_RUN_LIMIT:
            stale, _ = self.finished_runs.popitem(last=False)
            self.session_owners.pop(stale, None)

    def lookup_run(self, session_id):
        """Resolve a session to (bus, terminal) from active runs then finished ones."""
        if session_id in self.runs:
            bus, running = self.runs[session_id]
            return bus, running.done()
        bus = self.finished_runs.get(session_id)
        if bus is None:
            return None
        self.finished_runs.move_to_end(session_id)
        return bus, True

    def run_state(self, session_id):
        entry = self.runs.get(session_id)
        if entry is None:
            if session_id in self.finished_runs:
                return None, True
            return None, None
        bus, running = entry
        return bus, running.done()

    async def encrypt_stored_tokens(self):
        """Rewrite tokens written before they were encrypted.

        Idempotent through the marker, so it is a no-op after the first start. A
        row it cannot recover is a key the operator changed, which the read path
        reports rather than silently treating as "no token".
        """
        migrated = 0
        async for row in self.store.db.mcp_servers.find({"token": {"$exists": True}}):
            token = row.get("token")
            if isinstance(token, str) and token and not secrets_box.cipher().is_encrypted(token):
                await self.store.db.mcp_servers.update_one(
                    {"_id": row["_id"]}, {"$set": {"token": secrets_box.encrypt(token)}})
                migrated += 1
        if migrated:
            logger.info("Encrypted %d stored MCP token(s)", migrated)

    # --- identity ---------------------------------------------------------

    def guest_allowed(self, method: str, path: str) -> bool:
        # What an anonymous visitor may reach: reading the catalogue, running a
        # real turn, and answering the approval that turn raised. Anything that
        # changes the installation (a new agent, a skill, an MCP server, a
        # sandbox lease) is deliberately absent, so it answers 401 and the
        # interface asks to sign in.
        guest_routes = {
            ("GET", "/api/models"), ("GET", "/api/agents"),
            ("POST", "/api/agents/run"), ("POST", "/api/sessions"),
            ("POST", "/api/chat/completions"),
            ("GET", "/api/approvals"), ("GET", "/api/interventions/policy"),
        }
        if method in {"GET", "HEAD"} and path.startswith(("/api/sessions/", "/api/files/")):
            return True
        if method == "POST" and path.startswith(("/api/sessions/", "/api/approvals/")):
            # Stopping a run and answering its approval. Deleting a session is a
            # DELETE and stays behind the wall.
            return True
        return (method, path) in guest_routes

    async def bill_guest_turn(self):
        """Charge an anonymous turn to the visitor, before the model is called.

        Raises 403 with a machine-readable reason so the interface can open the
        login dialog instead of showing a generic failure.
        """
        identity = current_user.get()
        if not is_guest(identity):
            return
        refusal = await self.auth.claim_guest_turn(identity, self.settings.guest_turns,
                                                   self.settings.guest_daily_turns)
        if refusal:
            raise HTTPException(403, detail={"code": "guest_limit", "reason": refusal})

    def owner_id(self) -> str:
        """The calling account, from the cookie alone.

        Raises rather than returning a default: an empty identity here would
        silently turn an ownership filter into "everything", which is the exact
        failure this design is meant to prevent.
        """
        identity = current_user.get()
        if not identity:
            raise HTTPException(401, "Not authenticated.")
        return identity

    async def owned_session(self, identity: str) -> dict:
        """A session the caller owns, or the same 404 an unknown id gets.

        Answering 403 here would confirm that somebody else's session exists,
        which is exactly what a probing caller wants to learn.
        """
        session = await self.store.get("sessions", identity, owner=self.owner_id())
        if session is None:
            raise HTTPException(404, "Session not found.")
        return session

    def same_origin(self, request: Request) -> bool:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if not origin:
            # Browsers always send Origin on unsafe methods; curl and the test
            # client do not, and neither carries an ambient cookie from another site.
            return True
        allowed = {item.strip().rstrip("/")
                   for item in self.settings.allowed_origins.split(",") if item.strip()}
        if origin in allowed:
            return True
        # Behind a reverse proxy the Host header is the upstream, and the browser's
        # own hostname arrives as X-Forwarded-Host. Both are legitimate answers to
        # "which site is this request for".
        hosts = {request.headers.get("host", ""),
                 (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()}
        return origin.split("://", 1)[-1] in {host for host in hosts if host}

    def public_user(self, user: dict) -> dict:
        """What the browser may know about an account.

        The address and the number leave the server masked: the interface never
        needs either in full to show who is signed in, and the full value is
        exactly what a cross-site script would be after.
        """
        return {"id": user["id"], "username": user["username"],
                "displayName": user.get("display_name") or user["username"],
                "email": email_address.mask(user["email"]) if user.get("email") else "",
                "phone": phone_number.mask(user["phone"]) if user.get("phone") else ""}

    async def finish_login(self, user: dict, request: Request, response: Response):
        """Everything that happens once we know who is calling.

        One place, because a password login and a code login differ only in how
        they got here: both start a session, and both carry the visitor's trial
        transcript over instead of dropping it. Failing to move the transcript
        must not fail the login -- the account exists and the cookie is set.
        """
        token = await self.auth.start_session(user["id"])
        response.set_cookie(SESSION_COOKIE, token, max_age=int(self.settings.session_hours * 3600),
                            httponly=True, samesite="lax", secure=self.settings.cookie_secure,
                            path="/")
        guest = request.cookies.get(GUEST_COOKIE, "")
        if valid_guest_id(guest):
            try:
                await self.auth.adopt_guest(guest, user["id"])
                # The transcript may already own a sandbox from a pre-login turn.
                # Move the lease with the rows, or the next run refuses to resume
                # onto work this reader actually did.
                moved = await self.pool.adopt_owner(guest, user["id"])
                if moved:
                    logger.info("Re-bound %d sandbox lease(s) from %s to %s",
                                moved, guest, user["id"])
            except Exception:
                logger.warning("Could not adopt guest transcript %s", guest, exc_info=True)
            response.delete_cookie(GUEST_COOKIE, path="/")
        return {"code": 200, "data": self.public_user(user)}

    def client_ip(self, request: Request) -> str:
        """The caller's address, for the per-address send budget.

        Behind nginx the socket address is the proxy, so the forwarded header is
        the only thing that distinguishes two visitors. It is client-controlled
        and therefore only a budget key, never an authorisation.
        """
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "")

    # --- readiness --------------------------------------------------------

    def require_ready(self):
        if not self.app.state.ready:
            raise HTTPException(503, "MongoDB unavailable. Start docker compose up -d mongo and "
                                     "restart Harness.")

    def ready(self, model=False):
        if not self.app.state.ready:
            raise HTTPException(503, "MongoDB unavailable. Start docker compose up -d mongo and restart Harness.")
        adapter = self.gateway.adapters.get("deepseek")
        if model and isinstance(adapter, DeepSeekAdapter) and not adapter.api_key:
            raise HTTPException(503, "Configure the DeepSeek API key in the Harness environment and restart.")

    # --- files ------------------------------------------------------------

    def public_file(self, record: dict) -> dict:
        """What the browser needs to list and fetch a file -- never its path."""
        return {"fileId": record["id"], "name": record["name"], "size": record["size"],
                "mimeType": record["mimeType"], "createdAt": record["createdAt"],
                "downloadPath": f"/api/files/{record['id']}/download"}

    async def session_or_new(self, identity: str) -> dict | None:
        """The session if it exists and is ours; None while it is still a draft.

        The row is created by the first run, but the reader uploads while
        composing that first message, so a missing row has to be allowed.
        Files are keyed by (owner, session) anyway, which is what actually stops
        a stranger from reaching them; an existing row that belongs to somebody
        else is the usual 404.
        """
        session_files.session_identity(identity)
        row = await self.store.db.sessions.find_one({"id": identity}, {"_id": 0})
        if row is not None and row.get("owner_id") != self.owner_id():
            raise HTTPException(404, "Session not found.")
        return row

    async def owned_file(self, file_id: str):
        """The file this caller may read, after both locks, as (body, type, name).

        `body` is bytes for an artifact (stored in the row) or a `Path` for an
        uploaded blob (streamed from disk).
        """
        record = await self.store.get("artifacts", file_id, owner=self.owner_id())
        if record is None:
            # An uploaded file is fetched through the same URL, so the reader has
            # one way in regardless of who produced the bytes.
            uploaded = await self.store.get("session_files", file_id, owner=self.owner_id())
            if uploaded is None:
                raise HTTPException(404, "File not found.")
            # The row already came back inside this caller's ownership scope; the
            # session check is the second lock, and it has to tolerate a draft
            # conversation whose session row the first run has not created yet.
            await self.session_or_new(uploaded["sessionId"])
            path = self.file_store.blob(uploaded["sessionId"], uploaded["id"])
            if not path.is_file():
                raise HTTPException(404, "File not found.")
            return path, uploaded["mimeType"], uploaded["name"]
        # The artifact row was written by a run, so its session decides who may
        # read it: a stale row must not outlive the conversation's ownership.
        await self.owned_session(record.get("sessionId") or "")
        artifact = record["artifact"]
        return (bytes(record["content"]), artifact.get("mimeType") or "application/octet-stream",
                artifact["fileName"])

    # --- MCP --------------------------------------------------------------

    async def save_server(self, request: MCPServerRequest, allow_private: bool | None = None):
        """Discover once and store.

        Two properties belong to this boundary rather than to its callers, so a
        pasted config and a catalogue install cannot diverge: the address is
        checked before anything dials it, and the token is encrypted before it
        reaches the database. Nothing that leaves here contains the token, and
        neither does anything printed.
        """
        trusted = self.settings.mcp_allow_private if allow_private is None else allow_private
        try:
            private = await netguard.assert_public(request.url, allow_private=trusted)
        except netguard.BlockedAddress as exc:
            logger.warning("mcp registration refused id=%s url=%s reason=%s",
                           request.id, mask_url(request.url), exc)
            raise HTTPException(422, f"This deployment does not dial that address: {exc}") from None
        record = request.model_dump(exclude_none=True, exclude={"type", "headers"})
        # Recorded rather than re-derived later: the page labels the row with it,
        # and a name can start resolving somewhere else after it was registered.
        record["private"] = private
        existing = await self.store.get("mcp_servers", request.id)
        if request.enabled is None:
            # A refresh (same URL, new token) must not resurrect a server the
            # operator switched off.
            record["enabled"] = (existing or {}).get("enabled", True)
        try:
            tools = await server_tools(record, self.settings.mcp_discover_timeout,
                                       self.settings.mcp_call_timeout)
        except Exception as exc:
            logger.warning("mcp discovery failed id=%s url=%s error=%s", request.id,
                           mask_url(request.url), describe(exc))
            if status_code(exc) in {401, 403}:
                raise HTTPException(422, "MCP server rejected the access token. For a hosted "
                    "service, supply the token from the same account that deployed it.") from None
            raise HTTPException(502, "MCP discovery failed. Check the URL and the Streamable "
                "HTTP transport, and that the access token is valid.") from None
        stored = {**record, "token": secrets_box.encrypt(record.get("token"))}
        server = await self.store.put("mcp_servers", {**stored, "tools": [
            {"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]})
        logger.info("mcp server registered id=%s tools=%d token=%s private=%s", request.id,
                    len(tools), "yes" if record.get("token") else "no", private)
        return public_server(server)

    async def load_mcp_config(self, path):
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
        # A file in this directory is the operator's own list, so a sidecar on
        # loopback is a legitimate entry there and nowhere else.
        outcomes = await asyncio.gather(
            *(self.save_server(request, allow_private=True) for request in requests),
            return_exceptions=True)
        for request, outcome in zip(requests, outcomes):
            if isinstance(outcome, BaseException):
                reason = getattr(outcome, "detail", None) or describe(outcome)
                logger.warning("mcp server %s from config not registered: %s", request.id, reason)
            else:
                logger.info("mcp server %s from config ready tools=%d", outcome["id"],
                            len(outcome.get("tools", [])))

    async def load_mcp_catalog(self, path):
        """Publish the operator's service catalogue.

        A missing file is a valid state: the page then offers only what this
        account already installed, and an operator adds entries when there is
        something worth offering. Entries are shared rows (empty owner) because a
        catalogue is not per-account data, and `store.scope` makes them readable
        by everyone without making them writable.
        """
        items = parse_catalog(path)
        for item in items:
            await self.store.put("mcp_catalog", item.model_dump(), owner="")
        if items:
            logger.info("mcp catalogue published entries=%d", len(items))

    # --- agents -----------------------------------------------------------

    async def prepare_agent(self, agent):
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
            instructions = await self.skill_manager.instructions(agent.skills)
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
        selected, required = await run_servers(self.store, agent.mcp_servers,
                                               self.settings.mcp_auto_attach)
        attached = []
        for identity in selected:
            server = await self.store.get("mcp_servers", identity)
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
            server_tools(server, self.settings.mcp_discover_timeout, self.settings.mcp_call_timeout)
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

    async def validate_definition(self, agent: AgentDefinition) -> AgentDefinition:
        """Everything a stored definition must satisfy, checked the same way twice.

        Create and update share it: a definition that could not run is rejected at
        save time rather than at the first turn, when the failure would look like
        a runtime error to the person who just built it.
        """
        prepared, remote = await self.prepare_agent(agent)
        if agent.model.provider not in self.gateway.adapters:
            raise HTTPException(422, "Model provider is not registered.")
        try:
            # A fresh registry per call: the cached factory registry accumulates
            # remote tools across requests, so registering this agent's MCP tools
            # into it raises "Duplicate tool" the second time the same server is
            # used. Built-in names are copied in, nothing else is inherited.
            validation = ToolRegistry()
            for tool in self.tools_factory("validation").tools.values():
                if tool.source == "builtin":
                    validation.register(tool)
            for tool in remote:
                if tool.name not in validation.tools:
                    validation.register(tool)
            validation.schemas(prepared.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return prepared

    # --- execution --------------------------------------------------------

    async def execute(self, agent, task, session_id, bus, turn_id, remote, resume=False,
                      uploads=()):
        self.current_session.set(session_id)
        # The run lock recorded who owns this session when it was started. The
        # sandbox lease is bound to the same account, so a task created without
        # the request's context cannot quietly inherit someone else's container.
        lease_owner = self.session_owners.get(session_id, "") or self.owner_id()
        try:
            async with asyncio.timeout(900):
                previous = await self.store.get("sessions", session_id)
                isolated = agent.sandbox_policy is not None
                if previous and previous.get("isolated", False) != isolated:
                    raise ValueError("Start a new conversation to change execution environment.")
                if isolated:
                    for identity in (previous or {}).get("loaded_skills", []):
                        if identity not in agent.skills and len(agent.skills) < 8 and await self.store.get("skills", identity):
                            agent.skills.append(identity)
                registry = self.tools_factory(session_id)
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
                    lease = await self.pool.acquire(session_id, agent.sandbox_policy.image,
                                                    owner=lease_owner)
                    self.run_sandboxes[session_id] = lease.sandbox
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
                            registry.register(ECOM_TOOL_MAP[name](self.settings))
                            continue
                        if name not in {tool.name for tool in remote} | set(SKILL_TOOL_NAMES):
                            registry.register(SandboxTool(self.pool.runner, lease.sandbox, name,
                                                         agent.sandbox_policy.timeout))
                for name in SKILL_TOOL_NAMES:
                    registry.tools.pop(name, None)
                    registry.register(SkillTool(self.skill_manager, name, self.pool.runner,
                        self.run_sandboxes.get(session_id), agent, skill_loaded, skill_installed,
                        skill_deleted))
                for tool in remote:
                    registry.register(tool)
                self.isolated_tools[session_id] = registry
                await self.store.put("sessions", {**(previous or {}), "id": session_id,
                    "sessionId": session_id, "isolated": isolated})
                # The reader's files are copied into whichever workspace this run
                # actually uses -- host directory or sandbox -- because the two
                # trees are different and the model only sees the active one. The
                # copy is per turn, so a sandbox is available without teaching the
                # upload endpoint how a container is created.
                if uploads:
                    sandbox = self.run_sandboxes.get(session_id)
                    root = (sandbox.workspace.root if sandbox
                            else self.settings.agentforge_workspace / session_files.session_identity(session_id))
                    manifest = await asyncio.to_thread(self.file_store.sync_into, list(uploads), root)
                    agent = agent.model_copy(update={
                        "system_prompt": agent.system_prompt + session_files.manifest_prompt(manifest)})
                await self.runtime.run(agent, task, session_id, bus, turn_id=turn_id, resume=resume)
        except Exception:
            # exc_info matters here: the run is a background task, so without it a
            # failure is invisible and the user only sees a generic error event.
            logger.warning("Run failed for session %s", session_id, exc_info=True)
            if not bus.events or bus.events[-1]["type"] != "error":
                await bus.emit({"type": "error", "sessionId": session_id,
                                "message": "Run failed. Check configuration and retry."})
        finally:
            try:
                if session_id in self.isolated_tools:
                    current = await self.store.get("sessions", session_id)
                    if current:
                        await self.store.put("sessions", {**current, "loaded_skills": agent.skills})
            except Exception:
                logger.warning("Could not persist loaded skills for session %s", session_id)
            finally:
                self.isolated_tools.pop(session_id, None)
                self.run_sandboxes.pop(session_id, None)
                await self.pool.release(session_id)

    def start(self, agent, task, session_id, turn_id, remote=(), resume=False, uploads=()):
        if session_id in self.runs and not self.runs[session_id][1].done():
            raise HTTPException(409, "Session already has an active run.")
        self.session_owners[session_id] = self.owner_id()
        def workspace():
            sandbox = self.run_sandboxes.get(session_id)
            return sandbox.workspace if sandbox else Workspace(
                self.settings.agentforge_workspace / session_files.session_identity(session_id))
        bus = ArtifactEventBus(self.runtime, self.store, workspace)
        running = asyncio.create_task(
            self.execute(agent, task, session_id, bus, turn_id, remote, resume, uploads))
        running.add_done_callback(lambda task: self.retire_run(session_id, task, bus))
        self.runs[session_id] = (bus, running)
        return bus, running
