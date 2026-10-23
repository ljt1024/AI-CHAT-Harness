import asyncio
import json
from unittest.mock import MagicMock

import pytest

from agentforge.sandbox.docker_runner import DockerRunner


def test_container_hardening_and_zero_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_TEST_KEY", "do-not-pass")
    options = DockerRunner(tmp_path).options(tmp_path, "python:3.12-slim")
    assert options["read_only"] and options["cap_drop"] == ["ALL"]
    assert options["mem_limit"] == options["memswap_limit"] == "512m"
    assert options["pids_limit"] == 256 and options["nano_cpus"] == 1_000_000_000
    assert options["network_mode"] == "none"
    assert len(options["volumes"]) == 1
    assert "do-not-pass" not in str(options)
    assert not options["user"].startswith("0:")
    profile = json.loads(options["security_opt"][1].removeprefix("seccomp="))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {name for group in profile["syscalls"] if group["action"] == "SCMP_ACT_ALLOW" for name in group["names"]}
    assert not {"mount", "ptrace", "unshare", "setns", "bpf", "clone3"} & allowed
    with pytest.raises(ValueError):
        DockerRunner(tmp_path).options(tmp_path, "privileged-image")


async def test_exit_code_output_and_timeout(tmp_path):
    client = MagicMock()
    client.containers.run.return_value.id = "container"
    client.api.exec_create.return_value = {"Id": "exec"}
    client.api.exec_start.return_value = iter([b"x" * 10000])
    client.api.exec_inspect.return_value = {"ExitCode": 2}
    runner = DockerRunner(tmp_path, client)
    sandbox = await runner.create()
    result = await runner.execute(sandbox, ["false"])
    assert not result.ok and len(result.output) <= 4000
    def slow(*args, **kwargs):
        import time
        time.sleep(0.05)
        return iter([b"late"])
    client.api.exec_start.side_effect = slow
    result = await runner.execute(sandbox, ["sleep", "60"], timeout=0.01)
    assert not result.ok and "timed out" in result.error
    assert not sandbox.alive
    client.containers.run.return_value.remove.assert_called_once_with(force=True)


async def test_cancellation_destroys_container(tmp_path):
    client = MagicMock()
    runner = DockerRunner(tmp_path, client)
    sandbox = await runner.create()
    def slow(*args, **kwargs):
        import time
        time.sleep(0.05)
        return iter([])
    client.api.exec_start.side_effect = slow
    task = asyncio.create_task(runner.execute(sandbox, ["sleep", "60"]))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sandbox.alive
