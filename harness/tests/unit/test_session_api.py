import asyncio

import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import RunResult, ToolCall
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


def make_store():
    return MongoDBStore("", "test", client=AsyncMongoMockClient())


class Answer:
    """Immediately produces a final answer, no tool calls."""

    async def chat_stream(self, req):
        yield ContentDelta(text="hello")
        yield Done()


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_run_endpoint_can_resume_from_a_checkpoint(tmp_path, monkeypatch):
    """resume has to reach the runtime; before it did, checkpoint recovery was dead code."""
    import agentforge.api as api

    calls = []

    async def fake_run(self, agent, task, session_id, events, turn_id=None, resume=False):
        calls.append(resume)
        result = RunResult(trace_id="t", session_id=session_id, turn_id=turn_id or "turn",
                           output="resumed", stop_reason="completed")
        await events.emit({"type": "done", "sessionId": session_id, "result": result.model_dump()})
        return result

    monkeypatch.setattr(api.Runtime, "run", fake_run)
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     make_store(), gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            for index, resume in enumerate((False, True)):
                response = await client.post("/api/agents/run", json={
                    "input": "continue please", "sessionId": f"s{index}", "resume": resume})
                assert response.status_code == 200
    # The task the runtime sees came from the checkpoint, so let the call settle.
    await asyncio.sleep(0.05)
    assert calls == [False, True]


async def test_health_reports_every_dependency(tmp_path):
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     make_store(), gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = (await client.get("/health")).json()
    assert payload["mode"] == "runtime"
    assert payload["database"] is True
    # The old endpoint only reported the database, so a missing model key or a
    # stopped Docker daemon stayed invisible until a run failed.
    assert set(payload) >= {"status", "database", "model_key", "docker", "sandbox", "active_runs"}
    assert "states" in payload["sandbox"] and "circuit" in payload["sandbox"]
    assert payload["active_runs"] == 0


async def test_session_can_be_stopped_and_pending_approval_released(tmp_path):
    class AskThenAnswer:
        """First turn requests a confirmed tool; once it returns, just answer."""

        async def chat_stream(self, req):
            if any(m["role"] == "tool" for m in req.messages):
                yield ContentDelta(text="done")
            else:
                yield ToolCallRequest(call=ToolCall(name="run_command", arguments={"command": "pwd"}))
            yield Done()

    store = make_store()
    gateway = ModelGateway()
    gateway.register("deepseek", AskThenAnswer())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0)
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Shell", "tools": ["run_command"],
                "permissions": {"run_command": "confirm"}})
            assert created.status_code == 201
            agent_id = created.json()["data"]["id"]
            started = await client.post("/api/sessions", json={"agent_id": agent_id, "task": "Run pwd"})
            assert started.status_code == 201
            session_id = started.json()["session_id"]
            assert await wait_until(lambda: bool(app.state.approvals.pending))
            approval_id = next(iter(app.state.approvals.pending))
            listed = (await client.get("/api/approvals")).json()["data"]
            assert [item["id"] for item in listed] == [approval_id]

            stopped = await client.post(f"/api/sessions/{session_id}/stop")
            assert stopped.status_code == 200
            assert stopped.json()["data"]["cancelled_approvals"] == [approval_id]
            # The blocked approval is released and the run no longer lingers.
            assert not app.state.approvals.pending
            assert session_id not in app.state.runs
            assert session_id in app.state.finished_runs


async def test_session_context_reads_the_committed_reading(tmp_path):
    """The composer gauge asks for this after a reload instead of waiting for a run."""
    store = make_store()
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, gateway)
    async with app.router.lifespan_context(app):
        await store.put("sessions", {"id": "kept", "sessionId": "kept",
                                     "context": {"tokens": 1234, "window": 64000}})
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/sessions/kept/context")).json()["data"] == \
                {"tokens": 1234, "window": 64000}
            # A session that never ran has no reading; the caller hides the gauge.
            await store.put("sessions", {"id": "fresh", "sessionId": "fresh"})
            assert (await client.get("/api/sessions/fresh/context")).json()["data"] is None
            assert (await client.get("/api/sessions/absent/context")).status_code == 404


async def test_stopping_unknown_session_is_404(tmp_path):
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     make_store(), gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/sessions/nope/stop")).status_code == 404


async def test_session_delete_removes_derived_data(tmp_path):
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    store = make_store()
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Chat", "tools": []})
            assert created.status_code == 201
            agent_id = created.json()["data"]["id"]
            started = await client.post("/api/sessions", json={"agent_id": agent_id, "task": "Say hi"})
            session_id = started.json()["session_id"]
            # Let the run finish so it leaves a trace, turns and a session behind.
            assert await wait_until(lambda: session_id not in app.state.runs)
            assert await store.get("sessions", session_id)
            assert await store.list("traces", {"sessionId": session_id})

            assert (await client.get("/api/sessions")).status_code == 200
            assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 200
            assert await store.get("sessions", session_id) is None
            assert not await store.list("traces", {"sessionId": session_id})
            assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 404


async def test_finished_runs_stay_bounded(tmp_path):
    """Completed runs must be evicted, or a long-lived process retains every log."""
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    store = make_store()
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Chat", "tools": []})
            agent_id = created.json()["data"]["id"]
            oldest = None
            for index in range(24):
                started = await client.post("/api/sessions", json={"agent_id": agent_id, "task": f"Say hi {index}"})
                session_id = started.json()["session_id"]
                oldest = oldest or session_id
                assert await wait_until(lambda: session_id not in app.state.runs)
                assert not app.state.runs, "active runs must not accumulate"
            assert len(app.state.finished_runs) <= 16
            assert oldest not in app.state.finished_runs


async def test_approvals_endpoint_survives_database_outage(tmp_path):
    """Pending approvals are in-memory, so an already-authenticated caller keeps
    seeing them after the database goes away mid-run; identity cannot be
    re-checked from Mongo, but it was checked when the session was established."""
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    store = make_store()
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/approvals")).status_code == 200
            app.state.ready = False
            assert (await client.get("/api/approvals")).status_code == 200


async def test_deleting_a_session_discards_its_sandbox(tmp_path, monkeypatch):
    """A deleted conversation must not leave its container and files behind.

    The pool otherwise still knows the workspace, which keeps every sweep from
    reclaiming a directory holding this session's files.
    """
    from agentforge.sandbox.pool import SandboxPool

    discarded = []

    async def fake_discard(self, session_id):
        discarded.append(session_id)
        return True

    monkeypatch.setattr(SandboxPool, "discard", fake_discard)
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    store = make_store()
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await store.put("sessions", {"id": "throwaway", "sessionId": "throwaway"})
            assert (await client.delete("/api/sessions/throwaway")).status_code == 200
    assert discarded == ["throwaway"]
