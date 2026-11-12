import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from agentforge.gateway.base import ChatRequest, Chunk, ModelAdapter
from agentforge.gateway.retry import is_transient, wait_for
from agentforge.models import ModelRef

logger = logging.getLogger(__name__)


class ModelGateway:
    def __init__(self, retries: int = 0, backoff: float = 1.0, max_backoff: float = 8.0):
        self.adapters: dict[str, ModelAdapter] = {}
        self.fallback: dict[str, ModelRef] = {}
        # Zero by default: a hand-built gateway is a test double, and a test that
        # fails on purpose should not spend seconds sleeping between attempts.
        # The application passes the configured budget in.
        self.retries, self.backoff, self.max_backoff = retries, backoff, max_backoff

    def register(self, name: str, adapter: ModelAdapter) -> None:
        self.adapters[name] = adapter

    async def _stream(self, ref: ModelRef, messages, tools) -> AsyncIterator[Chunk]:
        request = ChatRequest(messages=messages, tools=tools, model_ref=ref)
        async for chunk in self.adapters[ref.provider].chat_stream(request):
            yield chunk

    async def _stream_with_retries(self, ref: ModelRef, messages, tools) -> AsyncIterator[Chunk]:
        attempt = 0
        while True:
            emitted = False
            try:
                async for chunk in self._stream(ref, messages, tools):
                    emitted = True
                    yield chunk
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                # Text already reached the reader, so a second attempt would
                # append a duplicate of what they are reading. Only a request
                # that failed before its first chunk is safe to repeat.
                if emitted or not is_transient(exc) or attempt >= self.retries:
                    raise
                delay = wait_for(exc, attempt, self.backoff, self.max_backoff)
                attempt += 1
                logger.warning("Model call failed provider=%s attempt=%d/%d retry_in=%.1fs error=%s: %s",
                               ref.provider, attempt, self.retries + 1, delay,
                               type(exc).__name__, exc)
                await asyncio.sleep(delay)

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
                async for chunk in self._stream_with_retries(ref, messages, tools):
                    emitted = True
                    yield chunk
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                # Never replay a partially streamed request against another
                # provider. Retries belong to one provider as well: the
                # fallback is a different model at a different price, so it
                # only runs once this provider's budget is spent.
                if emitted or ref.provider not in self.fallback:
                    raise
                ref = self.fallback[ref.provider]
