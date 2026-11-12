"""Conversations, runs and approvals.

The endpoints that start work, stream it, stop it, delete it, and let a human
answer what the gate is waiting on. They share the in-memory run table with
`AppDeps`, which is why the reads and writes below go through it rather than
through a store.
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from agentforge.api.deps import AppDeps
from agentforge.api.events import event_frames, legacy_result
from agentforge.api.schemas import ApprovalRequest, LegacyRequest, SessionRequest
from agentforge.models import AgentDefinition, ModelRef, SandboxPolicy, new_id
from agentforge import session_files

logger = logging.getLogger(__name__)


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    store, pool = d.store, d.pool
    approvals, policy = d.approvals, d.policy
    runs, finished_runs, session_owners = d.runs, d.finished_runs, d.session_owners
    isolated_tools, run_sandboxes, file_store = d.isolated_tools, d.run_sandboxes, d.file_store
    ready, owner_id, owned_session = d.ready, d.owner_id, d.owned_session
    bill_guest_turn, lookup_run, run_state = d.bill_guest_turn, d.lookup_run, d.run_state
    start, prepare_agent = d.start, d.prepare_agent

    @router.post("/api/agents/run")
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

    @router.post("/api/sessions", status_code=201)
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

    @router.get("/api/sessions/{session_id}/events")
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

    @router.get("/api/sessions/{session_id}/context")
    async def session_context(session_id: str):
        # The gauge reads this after a reload: the run's context events are gone, but
        # the last measurement was committed with the transcript it describes.
        ready()
        session = await store.get("sessions", session_id, owner=owner_id())
        if session is None:
            raise HTTPException(404, "Session not found.")
        return {"code": 200, "data": session.get("context")}

    @router.get("/api/sessions")
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

    @router.post("/api/sessions/{session_id}/stop")
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

    @router.delete("/api/sessions/{session_id}")
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

    @router.get("/api/approvals")
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

    @router.get("/api/interventions/policy")
    async def intervention_policy():
        # Answers "what does the system stop me for" without reading the source.
        return {"code": 200, "data": policy.explain()}

    @router.post("/api/approvals/{call_id}")
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

    @router.get("/api/traces/{session_id}")
    async def traces(session_id: str):
        ready()
        await owned_session(session_id)
        results = await store.list("traces", {"sessionId": session_id})
        for trace in results:
            trace["turns"] = sorted(await store.list("turns", {"trace_id": trace["id"]}), key=lambda t: t["index"])
            trace["tool_calls"] = await store.list("tool_calls", {"trace_id": trace["id"]})
        return {"sessionId": session_id, "traces": results}

    return router
