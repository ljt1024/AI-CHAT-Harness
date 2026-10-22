import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.events import EventBus
from agentforge.gateway.base import ContentDelta, Done, ModelOutputError, ToolCallRequest, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.loop import Runtime
from agentforge.models import AgentDefinition, StopConditions, ToolCall
from agentforge.store import MongoDBStore
from agentforge.tools.base import ToolRegistry
from agentforge.tools.fs import FileTool, Workspace


def runtime_for(tmp_path, adapter):
    gateway = ModelGateway()
    gateway.register("deepseek", adapter)
    registry = ToolRegistry()
    registry.register(FileTool(Workspace(tmp_path), "write_file"))
    return Runtime(gateway, MongoDBStore("", "test", client=AsyncMongoMockClient()), lambda _: registry)


async def test_recovery_discards_bad_batch_without_replaying_successful_writes(tmp_path):
    class WriteInChunks:
        calls = 0

        async def chat_stream(self, req):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": "index.html", "content": "first"}))
            elif self.calls == 2:
                yield UsageReport(tokens_in=10, tokens_out=4096, usd=0.01)
                yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": "index.html", "content": "BAD"}))
                raise ModelOutputError("output_limit", "length")
            elif self.calls == 3:
                assert "NONE of its tool calls were executed" in req.messages[-2]["content"]
                assert "Chinese (zh-CN)" in req.messages[-1]["content"]
                yield ToolCallRequest(call=ToolCall(name="write_file", arguments={
                    "path": "index.html", "content": "second", "append": True}))
            else:
                yield ContentDelta(text="完成")
            yield Done()

    runtime = runtime_for(tmp_path, WriteInChunks())
    approvals = []

    async def allow(agent, tool, call, emit):
        approvals.append(call)
        return True

    runtime.permission_check = allow
    events = EventBus()
    agent = AgentDefinition(name="test", tools=["write_file"])
    result = await runtime.run(agent, "生成网页", "s", events)
    assert result.stop_reason == "completed"
    assert result.cost.tokens_out == 4096
    assert (tmp_path / "index.html").read_text() == "firstsecond"
    assert [c.arguments["content"] for c in approvals] == ["first", "second"]
    assert len(await runtime.store.list("tool_calls")) == 2
    assert len([e for e in events.events if e["type"] == "model_recovery"]) == 1
    rejected = [t for t in await runtime.store.list("turns") if t.get("error_code")]
    assert rejected[0]["finish_reason"] == "length"
    history = (await runtime.store.get("sessions", "s"))["history"]
    assert not any("BAD" in str(m.get("tool_calls", [])) for m in history)


@pytest.mark.parametrize("max_turns,budget,expected_calls", [(25, 0.5, 3), (1, 0.5, 1), (25, 0.005, 1)])
async def test_output_recovery_is_bounded_and_never_executes_partial_calls(tmp_path, max_turns, budget, expected_calls):
    class Broken:
        calls = 0

        async def chat_stream(self, req):
            self.calls += 1
            yield UsageReport(tokens_in=10, tokens_out=4096, usd=0.01)
            yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": "bad", "content": "bad"}))
            raise ModelOutputError("invalid_tool_arguments", "tool_calls")

    adapter = Broken()
    runtime = runtime_for(tmp_path, adapter)
    agent = AgentDefinition(name="test", tools=["write_file"],
                            stop_conditions=StopConditions(max_turns=max_turns, budget=budget))
    events = EventBus()
    with pytest.raises(ModelOutputError):
        await runtime.run(agent, "生成网页", "s", events)
    assert adapter.calls == expected_calls
    assert not (tmp_path / "bad").exists()
    assert not await runtime.store.list("tool_calls")
    assert events.events[-1]["type"] == "error"
    assert events.events[-1]["code"] == "invalid_tool_arguments"
    assert "模型" in events.events[-1]["message"]
    trace = (await runtime.store.list("traces"))[0]
    assert trace["cost"]["tokens_out"] == 4096 * expected_calls
    assert trace["error_code"] == "invalid_tool_arguments"


async def test_checkpoint_resume_does_not_reset_output_recovery_limit(tmp_path):
    class BrokenThenDisconnected:
        calls = 0

        async def chat_stream(self, req):
            self.calls += 1
            yield UsageReport(tokens_in=1, tokens_out=1, usd=0.001)
            if self.calls == 3:
                raise httpx.ReadError("connection lost")
            raise ModelOutputError("output_limit", "length")

    adapter = BrokenThenDisconnected()
    runtime = runtime_for(tmp_path, adapter)
    agent = AgentDefinition(name="test", tools=["write_file"])
    with pytest.raises(httpx.ReadError):
        await runtime.run(agent, "生成网页", "s", EventBus())
    checkpoint = await runtime.store.latest_checkpoint("s")
    assert checkpoint["state"]["output_recoveries"] == 2
    events = EventBus()
    with pytest.raises(ModelOutputError):
        await runtime.run(agent, "unused", "s", events, resume=True)
    assert adapter.calls == 4
    assert not any(e["type"] == "model_recovery" for e in events.events)


async def test_last_response_has_explicit_finalization_budget(tmp_path):
    class Finish:
        async def chat_stream(self, req):
            assert "1 model response(s) remaining" in req.messages[-1]["content"]
            assert "honest final summary" in req.messages[-1]["content"]
            yield ContentDelta(text="完成")
            yield Done()

    runtime = runtime_for(tmp_path, Finish())
    result = await runtime.run(AgentDefinition(name="test", stop_conditions=StopConditions(max_turns=1)),
                               "生成网页", "s", EventBus())
    assert result.stop_reason == "completed"
    assert result.output == "完成"
