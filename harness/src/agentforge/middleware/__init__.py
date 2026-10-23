from typing import Protocol

from agentforge.tools.base import bounded


class Middleware(Protocol):
    name: str
    enabled: bool

    async def on_turn_start(self, ctx): ...
    async def on_tool_call(self, ctx, call): ...
    async def on_tool_result(self, ctx, result): ...
    async def on_turn_end(self, ctx): ...
    async def on_error(self, ctx, err): ...


class MiddlewareRejected(ValueError):
    pass


class Layer:
    enabled = True
    name = "layer"

    async def on_turn_start(self, ctx):
        return ctx

    async def on_tool_call(self, ctx, call):
        return call

    async def on_tool_result(self, ctx, result):
        return result

    async def on_turn_end(self, ctx):
        pass

    async def on_error(self, ctx, err):
        pass


class SandboxHealth(Layer):
    name = "SandboxHealth"

    def __init__(self, health_check=None):
        self.health_check = health_check
        self.enabled = health_check is not None

    async def on_turn_start(self, ctx):
        if not await self.health_check(ctx):
            raise MiddlewareRejected("Sandbox is unhealthy. Rebuild before continuing.")
        return ctx


class ContextInjection(Layer):
    name = "ContextInjection"

    def __init__(self, context: str = ""):
        self.context = context

    async def on_turn_start(self, ctx):
        message = {"role": "user", "content": f"Workspace context:\n{self.context}"}
        if self.context and message not in ctx.messages:
            ctx.messages.insert(1, message)
        return ctx


class SkillsSync(Layer):
    name = "SkillsSync"

    def __init__(self, sync=None):
        self.sync = sync
        self.enabled = sync is not None

    async def on_turn_start(self, ctx):
        await self.sync(ctx)
        return ctx


class ToolsSummarization(Layer):
    name = "ToolsSummarization"

    async def on_tool_result(self, ctx, result):
        result.output = bounded(result.output)
        if result.error:
            result.error = bounded(result.error)
        return result


class SandboxCircuitBreaker(Layer):
    name = "SandboxCircuitBreaker"

    def __init__(self, allow=None):
        self.allow = allow
        self.enabled = allow is not None

    async def on_tool_call(self, ctx, call):
        if not await self.allow(ctx):
            raise MiddlewareRejected("Sandbox circuit is open. Wait for recovery.")
        return call


class ModelCallLimit(Layer):
    name = "ModelCallLimit"

    def __init__(self, limit: int = 25):
        self.limit = limit

    async def on_turn_start(self, ctx):
        if ctx.model_calls >= self.limit:
            raise MiddlewareRejected("Model call limit reached.")
        ctx.model_calls += 1
        return ctx


class ToolCallLimit(Layer):
    name = "ToolCallLimit"

    def __init__(self, limit: int = 100):
        self.limit = limit

    async def on_tool_call(self, ctx, call):
        if ctx.tool_calls >= self.limit:
            raise MiddlewareRejected("Tool call limit reached.")
        ctx.tool_calls += 1
        return call


class MiddlewareStack:
    def __init__(self, layers=None):
        self.layers = layers if layers is not None else [SandboxHealth(), ContextInjection(), SkillsSync(),
            ToolsSummarization(), SandboxCircuitBreaker(), ModelCallLimit(), ToolCallLimit()]

    def active(self, reverse=False):
        layers = [layer for layer in self.layers if layer.enabled]
        return reversed(layers) if reverse else layers

    async def on_turn_start(self, ctx):
        ctx.metadata["middleware"] = []
        for layer in self.active():
            ctx = await layer.on_turn_start(ctx)
            ctx.metadata["middleware"].append(layer.name)
        return ctx

    async def on_tool_call(self, ctx, call):
        for layer in self.active():
            call = await layer.on_tool_call(ctx, call)
        return call

    async def on_tool_result(self, ctx, result):
        for layer in self.active(reverse=True):
            result = await layer.on_tool_result(ctx, result)
        return result

    async def on_turn_end(self, ctx):
        for layer in self.active(reverse=True):
            await layer.on_turn_end(ctx)

    async def on_error(self, ctx, err):
        for layer in self.active(reverse=True):
            await layer.on_error(ctx, err)
