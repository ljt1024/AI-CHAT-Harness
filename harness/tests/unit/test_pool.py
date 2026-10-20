import asyncio
from unittest.mock import MagicMock

import pytest

from agentforge.models import new_id
from agentforge.sandbox.breaker import CircuitBreaker, CircuitState
from agentforge.sandbox.docker_runner import Sandbox, SandboxUnavailable
from agentforge.sandbox.pool import SandboxPool, SandboxState
from agentforge.tools.fs import Workspace


class FakeRunner:
    def __init__(self, root):
        self.root, self.fail, self.created = root, False, []

    async def create(self, image="python:3.12-slim", workspace=None):
        if self.fail:
            raise SandboxUnavailable("Docker offline")
        identity = new_id()
        sandbox = Sandbox(identity, MagicMock(), workspace or Workspace(self.root / identity), image)
        self.created.append(sandbox)
        return sandbox

    async def healthy(self, sandbox):
        return sandbox.alive

    async def destroy(self, sandbox):
        sandbox.alive = False


def test_breaker_opens_and_allows_one_recovery_probe():
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, recovery_seconds=10, clock=lambda: now[0])
    breaker.failure()
    breaker.failure()
    assert breaker.state == CircuitState.OPEN
    with pytest.raises(SandboxUnavailable):
        breaker.before_call()
    now[0] = 11
    breaker.before_call()
    assert breaker.state == CircuitState.HALF_OPEN
    with pytest.raises(SandboxUnavailable):
        breaker.before_call()
    breaker.success()
    assert breaker.state == CircuitState.CLOSED


async def test_warm_claim_cache_isolation_expiry_and_restore(tmp_path):
    now = [0.0]
    pool = SandboxPool(FakeRunner(tmp_path), warm_size=2, max_size=5, idle_seconds=10,
                       clock=lambda: now[0])
    await pool.start()
    try:
        assert pool.status()["states"]["WARM"] == 2
        first = await pool.acquire("first")
        first.sandbox.workspace.path("result.txt").write_text("keep")
        assert first.state == SandboxState.CLAIMED
        with pytest.raises(SandboxUnavailable):
            await pool.acquire("first")
        await pool.release("first")
        assert first.state == SandboxState.CACHED
        same = await pool.acquire("first")
        assert same is first
        await pool.release("first")
        other = await pool.acquire("other")
        assert other.sandbox.workspace.root != first.sandbox.workspace.root
        now[0] = 11
        await pool.reap()
        assert not first.sandbox.alive
        restored = await pool.acquire("first")
        assert restored.sandbox.workspace.path("result.txt").read_text() == "keep"
    finally:
        await pool.close()
    assert not pool.entries


async def test_concurrent_capacity_and_create_failure_recovery(tmp_path):
    runner = FakeRunner(tmp_path)
    pool = SandboxPool(runner, warm_size=0, max_size=1)
    try:
        results = await asyncio.gather(pool.acquire("a"), pool.acquire("b"), return_exceptions=True)
        assert sum(isinstance(r, SandboxUnavailable) for r in results) == 1
        assert len(pool.entries) == 1
    finally:
        await pool.close()
    failing = SandboxPool(FakeRunner(tmp_path), warm_size=1)
    failing.runner.fail = True
    await failing.replenish()
    assert failing.creating == 0
    assert failing.status()["last_error"] == "Docker offline"
    failing.runner.fail = False
    await failing.replenish()
    assert failing.status()["states"]["WARM"] == 1
    await failing.close()


async def test_same_session_cannot_create_two_containers(tmp_path):
    class SlowRunner(FakeRunner):
        async def create(self, *args, **kwargs):
            await asyncio.sleep(0.02)
            return await super().create(*args, **kwargs)
    pool = SandboxPool(SlowRunner(tmp_path), warm_size=0, max_size=4)
    try:
        results = await asyncio.gather(pool.acquire("same"), pool.acquire("same"), return_exceptions=True)
        assert sum(isinstance(result, SandboxUnavailable) for result in results) == 1
        assert len(pool.entries) == 1
        assert not pool.pending_sessions
    finally:
        await pool.close()
