import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient

from agentforge.events import EventBus
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime, SessionBusy
from agentforge.models import AgentDefinition, StopConditions, ToolCall
from agentforge.store import MongoDBStore
from agentforge.tools.base import ToolRegistry
from agentforge.tools.fs import FileTool, Workspace


class Script:
    def __init__(self, kind):
        self.kind = kind
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_stream(self, req):
        self.entered.set()
        if self.kind == "block":
            await self.release.wait()
        if self.kind in {"loop", "budget", "max"}:
            yield ToolCallRequest(call=ToolCall(name="glob", arguments={"pattern": "*"}))
            if self.kind == "budget":
                yield UsageReport(tokens_in=10, tokens_out=10, usd=1)
        else:
            yield ContentDelta(text="finished")
        yield Done()


def make_runtime(tmp_path, kind):
    adapter, gateway = Script(kind), ModelGateway()
    gateway.register("deepseek", adapter)
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    tools = ToolRegistry()
    tools.register(FileTool(Workspace(tmp_path), "glob"))
    return Runtime(gateway, store, lambda _: tools), adapter


@pytest.mark.parametrize("kind,reason,turns", [
    ("done", "completed", 1), ("budget", "budget", 1), ("max", "max_turns", 2), ("loop", "idle", 10),
])
async def test_stop_paths(tmp_path, kind, reason, turns):
    runtime, _ = make_runtime(tmp_path, kind)
    agent = AgentDefinition(name="test", tools=["glob"], stop_conditions=StopConditions(max_turns=turns))
    events = EventBus()
    result = await runtime.run(agent, "task", "session", events)
    assert result.stop_reason == reason
    assert (await runtime.store.get("traces", result.trace_id))["cost"] == result.cost.model_dump()
    if kind == "loop":
        assert len([e for e in events.events if e["type"] == "reflection"]) == 1
        assert await runtime.store.latest_checkpoint("session")


async def test_session_concurrency_and_history(tmp_path):
    runtime, adapter = make_runtime(tmp_path, "block")
    agent = AgentDefinition(name="test")
    first = asyncio.create_task(runtime.run(agent, "first", "s", EventBus()))
    await adapter.entered.wait()
    with pytest.raises(SessionBusy):
        await runtime.run(agent, "second", "s", EventBus())
    adapter.release.set()
    await first
    session = await runtime.store.get("sessions", "s")
    assert session["history"][-1]["content"] == "finished"
    assert not runtime.active


async def test_orderly_stops_commit_history_but_crashes_do_not(tmp_path):
    """Budget, turn and idle stops are resumable context; a crash is not."""
    runtime, _ = make_runtime(tmp_path, "budget")
    agent = AgentDefinition(name="test", tools=["glob"], stop_conditions=StopConditions(max_turns=5))
    result = await runtime.run(agent, "task", "budgeted", EventBus())
    assert result.stop_reason == "budget"
    history = (await runtime.store.get("sessions", "budgeted"))["history"]
    assert any(m["role"] == "user" and m["content"] == "task" for m in history)
    assert any(m["role"] == "tool" for m in history)
    # The next turn must be able to continue from what was learned.
    assert "lastTurnId" not in await runtime.store.get("sessions", "budgeted")

    class Boom:
        async def chat_stream(self, req):
            raise RuntimeError("crash")
            yield ContentDelta(text="unreachable")  # keeps this an async generator

    crashed, _ = make_runtime(tmp_path, "done")
    crashed.gateway.register("deepseek", Boom())
    with pytest.raises(RuntimeError):
        await crashed.run(AgentDefinition(name="test", tools=["glob"]), "task", "crashed", EventBus())
    assert "history" not in await crashed.store.get("sessions", "crashed")


async def test_resume_without_a_checkpoint_says_what_is_missing(tmp_path):
    """The runtime raises these itself, so the user should get the reason, not a shrug."""
    runtime, _ = make_runtime(tmp_path, "done")
    events = EventBus()
    with pytest.raises(ValueError):
        await runtime.run(AgentDefinition(name="test"), "task", "empty", events, resume=True)
    error = [event for event in events.events if event["type"] == "error"][-1]
    assert error["message"] == "No matching checkpoint exists for this session."


async def test_current_tool_manifest_overrides_stale_history_without_accumulating(tmp_path):
    requests = []

    class Capture:
        async def chat_stream(self, req):
            requests.append(req)
            yield ContentDelta(text="Current tools checked")
            yield Done()

    runtime, _ = make_runtime(tmp_path, "done")
    runtime.gateway.register("deepseek", Capture())
    agent = AgentDefinition(name="test", tools=["glob"])
    stale = {"role": "assistant", "content": "I have no tools and cannot search files."}
    await runtime.store.put("sessions", {"id": "old", "history": [stale]})
    for task in ("Search files", "Search again"):
        await runtime.run(agent, task, "old", EventBus())
        request = requests[-1]
        manifest = request.messages[-1]
        assert manifest["role"] == "system"
        assert "- glob:" in manifest["content"]
        assert "- install_skill:" not in manifest["content"]
        assert "outdated" in manifest["content"]
        assert request.messages[-2] == {"role": "user", "content": task}
        assert [s["function"]["name"] for s in request.tools] == ["glob"]
        history = (await runtime.store.get("sessions", "old"))["history"]
        assert stale in history
        assert not any(m["role"] == "system" for m in history)


async def test_resume_after_failure_preserves_trace_and_committed_history(tmp_path):
    class CrashAfterCheckpoint:
        async def chat_stream(self, req):
            count = sum(m["role"] == "tool" for m in req.messages)
            if count == 5:
                raise RuntimeError("simulated crash")
            yield ToolCallRequest(call=ToolCall(name="glob", arguments={"pattern": str(count)}))
            yield UsageReport(tokens_in=2, tokens_out=1, usd=0.001)
            yield Done()
    runtime, _ = make_runtime(tmp_path, "done")
    runtime.gateway.register("deepseek", CrashAfterCheckpoint())
    agent = AgentDefinition(name="test", tools=["glob"])
    with pytest.raises(RuntimeError):
        await runtime.run(agent, "task", "s", EventBus())
    checkpoint = await runtime.store.latest_checkpoint("s")
    assert checkpoint["state"]["index"] == 5
    assert "history" not in await runtime.store.get("sessions", "s")
    class Finish:
        async def chat_stream(self, req):
            assert "Chinese (zh-CN)" not in req.messages[0]["content"]
            yield ContentDelta(text="finished")
            yield Done()

    runtime.gateway.register("deepseek", Finish())
    result = await runtime.run(agent, "恢复参数不应改变原任务的语言", "s", EventBus(), resume=True)
    assert result.stop_reason == "completed"
    assert result.trace_id == checkpoint["state"]["trace_id"]
    assert result.cost.usd == pytest.approx(0.005)
    assert len(await runtime.store.list("traces")) == 1
