import json
from collections.abc import AsyncIterator

import httpx

from agentforge.gateway.base import (
    ChatRequest, Chunk, ContentDelta, Done, Pricing, ToolCallRequest, UsageReport,
)
from agentforge.models import ToolCall


class DeepSeekAdapter:
    pricing = Pricing()

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[Chunk]:
        if not self.api_key:
            raise ValueError("Configure the model API key in the Harness environment.")
        params = {k: v for k, v in req.model_ref.params.items()
                  if k in {"temperature", "top_p", "max_tokens", "seed"}}
        body = {**params, "model": req.model_ref.model_id, "messages": req.messages,
                "stream": True, "stream_options": {"include_usage": True}}
        body.setdefault("max_tokens", 4096)
        if req.tools:
            body["tools"] = req.tools
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=60)
        calls: dict[int, dict] = {}
        completed = False
        try:
            async with client.stream("POST", "https://api.deepseek.com/chat/completions",
                                     json=body, headers={"Authorization": f"Bearer {self.api_key}"}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    payload = json.loads(data)
                    usage = payload.get("usage")
                    if usage:
                        count_in, count_out = usage["prompt_tokens"], usage["completion_tokens"]
                        yield UsageReport(tokens_in=count_in, tokens_out=count_out,
                                          usd=(count_in * self.pricing.input_per_million +
                                               count_out * self.pricing.output_per_million) / 1e6)
                    for choice in payload.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            yield ContentDelta(text=delta["content"])
                        for item in delta.get("tool_calls", []):
                            call = calls.setdefault(item["index"], {"id": "", "name": "", "args": ""})
                            if item.get("id"):
                                call["id"] = item["id"]
                            function = item.get("function", {})
                            call["name"] += function.get("name", "")
                            call["args"] += function.get("arguments", "")
                if not completed:
                    raise httpx.ReadError("Model stream ended without its completion marker.")
                for item in calls.values():
                    yield ToolCallRequest(call=ToolCall(id=item["id"], name=item["name"],
                                                       arguments=json.loads(item["args"] or "{}")))
                yield Done()
        finally:
            if own_client:
                await client.aclose()
