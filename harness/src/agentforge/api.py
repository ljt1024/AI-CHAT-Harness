import asyncio
from contextlib import asynccontextmanager
import json
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from agentforge.config import Settings
from agentforge.context import ContextManager
from agentforge.events import EventBus
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime
from agentforge.middleware import MiddlewareStack
from agentforge.models import AgentDefinition, ModelRef, new_id
from agentforge.permissions import ApprovalManager
from agentforge.store import MongoDBStore
from agentforge.tools.base import ToolRegistry
from agentforge.tools.fs import FileTool, Workspace
from agentforge.tools.shell import ShellTool

logger = logging.getLogger(__name__)


class LegacyRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100000)
    sessionId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turnId: str = Field(default_factory=new_id, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    stream: bool = False
    agentIds: list[str] = Field(default_factory=list, max_length=1)
    model: str = "deepseek-chat"
    fileIds: list[str] = Field(default_factory=list)


class SessionRequest(BaseModel):
    agent_id: str
    task: str = Field(min_length=1, max_length=100000)


class ApprovalRequest(BaseModel):
    approved: StrictBool
    reason: str = Field(default="", max_length=2000)


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
            "cost": result["cost"], "stop_reason": result["stop_reason"]}


def legacy_event(event, task):
    event = event.copy()
    if event["type"] == "turn_started":
        event["type"] = "start"
    elif event["type"] == "content":
        event["type"] = "delta"
    elif event["type"] == "tool_call":
        call = event["call"]
        event.update(type="step", step={"id": call["id"], "toolCallId": call["id"], "phase": "action",
            "status": "running", "output": f"{call['name']} {json.dumps(call['arguments'])}"})
    elif event["type"] == "tool_result":
        result = event["result"]
        event.update(type="step", step={"id": event["call_id"], "toolCallId": event["call_id"],
            "phase": "observation", "status": "completed" if result["ok"] else "failed",
            "output": result["output"] or result["error"] or ""})
    elif event["type"] == "done":
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
    store = store if store is not None else MongoDBStore(settings.mongodb_uri, settings.agentforge_db)
    gateway = gateway or ModelGateway()
    if "deepseek" not in gateway.adapters:
        gateway.register("deepseek", DeepSeekAdapter(settings.deepseek_api_key.get_secret_value()))

    def tools_factory(session_id):
        workspace = Workspace(settings.agentforge_workspace / session_id)
        registry = ToolRegistry()
        for name in ("read_file", "write_file", "grep", "glob"):
            registry.register(FileTool(workspace, name))
        registry.register(ShellTool(workspace))
        return registry

    runtime = Runtime(gateway, store, tools_factory)
    runtime.middleware, runtime.context_manager = MiddlewareStack(), ContextManager()
    approvals = ApprovalManager(store, settings.approval_timeout)
    runtime.permission_check = approvals.check
    runs = {}
    default = AgentDefinition(id="default", name="AgentForge", tools=["read_file", "write_file", "grep", "glob", "run_command"])

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        try:
            await store.setup()
            if await store.get("agents", "default") is None:
                await store.put("agents", default.model_dump())
            app.state.ready = True
        except Exception:
            logger.warning("MongoDB unavailable; data endpoints return 503.")
        yield
        for _, task in runs.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in runs.values()), return_exceptions=True)

    app = FastAPI(title="AgentForge", lifespan=lifespan)
    app.state.runtime, app.state.approvals = runtime, approvals

    def ready(model=False):
        if not app.state.ready:
            raise HTTPException(503, "MongoDB unavailable. Start docker compose up -d mongo and restart Harness.")
        adapter = gateway.adapters.get("deepseek")
        if model and isinstance(adapter, DeepSeekAdapter) and not adapter.api_key:
            raise HTTPException(503, "Configure the DeepSeek API key in the Harness environment and restart.")

    @app.get("/health")
    async def health():
        return {"status": "ok" if app.state.ready else "degraded", "database": app.state.ready, "mode": "runtime"}

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

    @app.post("/api/agents", status_code=201)
    async def create_agent(agent: AgentDefinition):
        ready()
        if agent.sandbox_policy is not None or agent.skills:
            raise HTTPException(422, "Sandbox and skills require Tier 2.")
        if agent.model.provider not in gateway.adapters:
            raise HTTPException(422, "Model provider is not registered.")
        try:
            tools_factory("validation").schemas(agent.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if await store.get("agents", agent.id):
            raise HTTPException(409, "Agent ID already exists.")
        return {"code": 201, "data": await store.put("agents", agent.model_dump())}

    async def execute(agent, task, session_id, bus, turn_id):
        try:
            async with asyncio.timeout(900):
                await runtime.run(agent, task, session_id, bus, turn_id=turn_id)
        except Exception:
            logger.warning("Run failed for session %s", session_id)
            if not bus.events or bus.events[-1]["type"] != "error":
                await bus.emit({"type": "error", "sessionId": session_id,
                                "message": "Run failed. Check configuration and retry."})

    def start(agent, task, session_id, turn_id):
        if session_id in runs and not runs[session_id][1].done():
            raise HTTPException(409, "Session already has an active run.")
        bus = EventBus(runtime)
        running = asyncio.create_task(execute(agent, task, session_id, bus, turn_id))
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
        bus, running = start(agent, request.input, request.sessionId, request.turnId)
        if not request.stream:
            await running
            final = bus.events[-1]
            if final["type"] == "error":
                raise HTTPException(502, final["message"])
            return {"code": 200, "data": legacy_result(final["result"], request.input)}
        async def frames():
            try:
                async for frame in event_frames(bus, request.input, legacy=True):
                    yield frame
            finally:
                if not running.done():
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
        bus, _ = start(AgentDefinition.model_validate(definition), request.task, session_id, turn_id)
        async with bus.condition:
            await bus.condition.wait_for(lambda: bool(bus.events))
        return {"session_id": session_id, "turn_id": turn_id, "trace_id": bus.events[0].get("trace_id")}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, after: int = Query(0, ge=0)):
        if session_id not in runs:
            raise HTTPException(404, "Session events are unavailable in this process.")
        bus, running = runs[session_id]
        if after >= len(bus.events) and running.done():
            return StreamingResponse(iter([]), media_type="text/event-stream")
        return StreamingResponse(event_frames(bus, after=after), media_type="text/event-stream")

    @app.post("/api/approvals/{call_id}")
    async def approve(call_id: str, request: ApprovalRequest):
        try:
            approvals.resolve(call_id, request.approved, request.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": request.approved}

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
            async for chunk in gateway.chat([m.model_dump() for m in request.messages], [], ModelRef(model_id=request.model)):
                if chunk.type == "content":
                    payload = {"choices": [{"delta": {"content": chunk.text}}]}
                elif chunk.type == "usage":
                    payload = {"choices": [], "usage": {"prompt_tokens": chunk.tokens_in,
                        "completion_tokens": chunk.tokens_out, "total_tokens": chunk.tokens_in + chunk.tokens_out}}
                else:
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream")

    return app
