import time

import docker
from docker.errors import DockerException
import pytest

from agentforge.sandbox.docker_runner import DockerRunner
from agentforge.sandbox.pool import SandboxPool


async def test_real_prewarmed_claim_reuse_reap_and_rebuild(tmp_path):
    try:
        client = docker.from_env(timeout=5)
        client.images.get("python:3.12-slim")
    except DockerException as exc:
        pytest.skip(f"Docker image required: {type(exc).__name__}")
    now = [0.0]
    pool = SandboxPool(DockerRunner(tmp_path, client), warm_size=2, max_size=5,
                       idle_seconds=10, clock=lambda: now[0])
    await pool.start()
    try:
        started = time.perf_counter()
        lease = await pool.acquire("session")
        elapsed = time.perf_counter() - started
        assert elapsed <= 0.5, f"Prewarmed acquisition took {elapsed:.3f}s"
        assert (await pool.runner.execute(lease.sandbox, ["sh", "-c", "echo preserved > /workspace/file.txt"])).ok
        await pool.release("session")
        assert (await pool.acquire("session")).sandbox.id == lease.sandbox.id
        await pool.release("session")
        now[0] = 11
        await pool.reap()
        assert not lease.sandbox.alive
        rebuilt = await pool.acquire("session")
        result = await pool.runner.execute(rebuilt.sandbox, ["cat", "/workspace/file.txt"])
        assert result.output.strip() == "preserved"
    finally:
        await pool.close()
        client.close()
