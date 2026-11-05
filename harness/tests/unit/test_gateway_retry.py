"""A transient provider failure must cost a retry, not the user's turn."""

import httpx
import pytest

from agentforge.gateway.base import ContentDelta, Done
from agentforge.gateway.registry import ModelGateway
from agentforge.gateway.retry import is_transient, wait_for
from agentforge.models import ModelRef


def status_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test/chat/completions")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class Flaky:
    """Fails a fixed number of times before answering, counting every attempt."""

    def __init__(self, failures: int = 0, error: Exception | None = None, partial: bool = False):
        self.failures, self.error, self.partial = failures, error, partial
        self.attempts = 0

    async def chat_stream(self, req):
        self.attempts += 1
        if self.attempts <= self.failures:
            if self.partial:
                yield ContentDelta(text="partial")
            raise self.error
        yield ContentDelta(text="ok")
        yield Done()


def gateway_with(retries: int, primary: Flaky, **kwargs) -> ModelGateway:
    # backoff=0 keeps the retry path under test without spending wall-clock time.
    gateway = ModelGateway(retries=retries, backoff=0, **kwargs)
    gateway.register("deepseek", primary)
    return gateway


async def test_a_rate_limit_is_retried_and_then_answers():
    primary = Flaky(failures=1, error=status_error(429))
    chunks = [c async for c in gateway_with(2, primary).chat([], [], ModelRef())]
    assert primary.attempts == 2
    assert [c.type for c in chunks] == ["content", "done"]


async def test_a_bad_request_is_not_retried():
    primary = Flaky(failures=9, error=status_error(400))
    with pytest.raises(httpx.HTTPStatusError):
        _ = [c async for c in gateway_with(3, primary).chat([], [], ModelRef())]
    assert primary.attempts == 1


async def test_partial_output_is_never_replayed():
    primary = Flaky(failures=9, error=status_error(503), partial=True)
    seen = []
    with pytest.raises(httpx.HTTPStatusError):
        async for chunk in gateway_with(3, primary).chat([], [], ModelRef()):
            seen.append(chunk.text)
    assert primary.attempts == 1
    assert seen == ["partial"]


async def test_retries_stop_at_the_configured_budget():
    primary = Flaky(failures=9, error=status_error(500))
    with pytest.raises(httpx.HTTPStatusError):
        _ = [c async for c in gateway_with(2, primary).chat([], [], ModelRef())]
    assert primary.attempts == 3


async def test_the_fallback_runs_only_after_the_retry_budget_is_spent():
    primary = Flaky(failures=9, error=status_error(503))
    backup = Flaky()
    gateway = gateway_with(1, primary)
    gateway.register("backup", backup)
    gateway.fallback["deepseek"] = ModelRef(provider="backup")
    chunks = [c async for c in gateway.chat([], [], ModelRef())]
    assert primary.attempts == 2
    assert backup.attempts == 1
    assert chunks[-1].type == "done"


async def test_a_gateway_without_a_budget_keeps_the_old_behaviour():
    # Test doubles are hand-built; they must not silently sleep or retry.
    primary = Flaky(failures=1, error=status_error(429))
    gateway = ModelGateway()
    gateway.register("deepseek", primary)
    with pytest.raises(httpx.HTTPStatusError):
        _ = [c async for c in gateway.chat([], [], ModelRef())]
    assert primary.attempts == 1


@pytest.mark.parametrize("status,transient", [
    (408, True), (409, True), (425, True), (429, True),
    (500, True), (502, True), (503, True), (504, True),
    (400, False), (401, False), (402, False), (403, False), (404, False), (422, False),
])
def test_which_statuses_are_worth_repeating(status, transient):
    assert is_transient(status_error(status)) is transient


def test_transport_failures_are_transient():
    assert is_transient(httpx.ConnectError("offline"))
    assert is_transient(httpx.ReadTimeout("slow"))


def test_backoff_doubles_and_stops_at_the_ceiling():
    error = status_error(503)
    assert [wait_for(error, attempt, 1.0, 8.0) for attempt in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_a_retry_after_hint_wins_over_the_backoff():
    assert wait_for(status_error(429, {"retry-after": "3"}), 0, 1.0, 8.0) == 3
    # ... but never beyond the ceiling the operator set.
    assert wait_for(status_error(429, {"retry-after": "120"}), 0, 1.0, 8.0) == 8


def test_an_unusable_retry_after_falls_back_to_the_backoff():
    assert wait_for(status_error(429, {"retry-after": "soon"}), 1, 1.0, 8.0) == 2.0
    # A date without a zone cannot be trusted to be an absolute instant.
    assert wait_for(status_error(429, {"retry-after": "Wed, 21 Oct 2015 07:28:00 -0000"}),
                    0, 1.0, 8.0) == 1.0
