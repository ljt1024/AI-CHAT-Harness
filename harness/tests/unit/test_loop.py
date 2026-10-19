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
