from collections.abc import AsyncIterator

import httpx

from agentforge.gateway.base import ChatRequest, Chunk, ModelAdapter
from agentforge.models import ModelRef


class ModelGateway:
    def __init__(self):
        self.adapters: dict[str, ModelAdapter] = {}
        self.fallback: dict[str, ModelRef] = {}

    def register(self, name: str, adapter: ModelAdapter) -> None:
        self.adapters[name] = adapter

    async def chat(self, messages, tools, model_ref: ModelRef) -> AsyncIterator[Chunk]:
        ref = model_ref
        seen: set[str] = set()
        while True:
            if ref.provider not in self.adapters:
                raise ValueError(f"Model provider '{ref.provider}' is not registered.")
            if ref.provider in seen:
                raise ValueError("Model fallback cycle detected.")
            seen.add(ref.provider)
            emitted = False
            try:
                req = ChatRequest(messages=messages, tools=tools, model_ref=ref)
                async for chunk in self.adapters[ref.provider].chat_stream(req):
                    emitted = True
                    yield chunk
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                # Never replay a partially streamed request against another provider.
                if emitted or ref.provider not in self.fallback:
                    raise
                ref = self.fallback[ref.provider]
