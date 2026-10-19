import json

import httpx
import pytest

from agentforge.gateway.base import ChatRequest, ContentDelta, Done
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ModelRef


async def test_deepseek_fragmented_tools_and_usage():
    frames = [
        {"choices": [{"delta": {"content": "hello", "tool_calls": [
            {"index": 0, "id": "call-1", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    def handler(request):
        body = json.loads(request.content)
        assert body["stream_options"]["include_usage"]
        return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames)
                              + "data: [DONE]\n\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = DeepSeekAdapter("test-key", client)
        chunks = [c async for c in adapter.chat_stream(ChatRequest(messages=[]))]
    assert chunks[0].text == "hello"
    usage = next(c for c in chunks if c.type == "usage")
    assert usage.usd == pytest.approx(0.0000364)
    assert next(c for c in chunks if c.type == "tool_call").call.arguments == {"path": "a.txt"}


class Adapter:
    def __init__(self, fail=False, partial=False):
        self.fail, self.partial = fail, partial

    async def chat_stream(self, req):
        if self.partial:
            yield ContentDelta(text="partial")
        if self.fail:
            raise httpx.ConnectError("offline")
        yield Done()


async def test_registry_and_fallback():
    gateway = ModelGateway()
    with pytest.raises(ValueError, match="not registered"):
        _ = [c async for c in gateway.chat([], [], ModelRef())]
    gateway.register("deepseek", Adapter(fail=True))
    gateway.register("backup", Adapter())
    gateway.fallback["deepseek"] = ModelRef(provider="backup")
    assert len([c async for c in gateway.chat([], [], ModelRef())]) == 1


async def test_no_fallback_after_partial_output():
    gateway = ModelGateway()
    gateway.register("deepseek", Adapter(fail=True, partial=True))
    gateway.register("backup", Adapter())
    gateway.fallback["deepseek"] = ModelRef(provider="backup")
    with pytest.raises(httpx.ConnectError):
        _ = [c async for c in gateway.chat([], [], ModelRef())]


async def test_truncated_provider_stream_is_not_success():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: {"choices":[]}\n\n')
    )) as client:
        with pytest.raises(httpx.ReadError):
            _ = [c async for c in DeepSeekAdapter("test", client).chat_stream(ChatRequest(messages=[]))]
