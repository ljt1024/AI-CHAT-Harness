import pytest

from agentforge.loop import TurnContext
from agentforge.middleware import Layer, MiddlewareRejected, MiddlewareStack, ModelCallLimit, ToolCallLimit
from agentforge.models import AgentDefinition, Cost, ToolCall


async def test_onion_order_and_toggle():
    order = []
    class Recorder(Layer):
        def __init__(self, name):
            self.name = name

        async def on_turn_start(self, ctx):
            order.append(self.name + ":start")
            return ctx

        async def on_turn_end(self, ctx):
            order.append(self.name + ":end")
    a, b, c = Recorder("a"), Recorder("b"), Recorder("c")
    b.enabled = False
    stack = MiddlewareStack([a, b, c])
    ctx = TurnContext(AgentDefinition(name="test"), [], 0, Cost())
    await stack.on_turn_start(ctx)
    await stack.on_turn_end(ctx)
    assert order == ["a:start", "c:start", "c:end", "a:end"]


async def test_limits_and_seven_layers():
    stack = MiddlewareStack()
    assert [layer.name for layer in stack.layers] == ["SandboxHealth", "ContextInjection", "SkillsSync",
        "ToolsSummarization", "SandboxCircuitBreaker", "ModelCallLimit", "ToolCallLimit"]
    ctx = TurnContext(AgentDefinition(name="test"), [], 0, Cost())
    limit = ModelCallLimit(1)
    await limit.on_turn_start(ctx)
    with pytest.raises(MiddlewareRejected):
        await limit.on_turn_start(ctx)
    calls = ToolCallLimit(1)
    await calls.on_tool_call(ctx, ToolCall(name="glob"))
    with pytest.raises(MiddlewareRejected):
        await calls.on_tool_call(ctx, ToolCall(name="glob"))
