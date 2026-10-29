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


async def test_destroy_keeps_the_workspace_for_a_rebuild(tmp_path):
    client = MagicMock()
    runner = DockerRunner(tmp_path, client)
    sandbox = await runner.create()
    sandbox.workspace.path("note.txt").write_text("x")
    await runner.destroy(sandbox)
    # An idle sandbox is reaped and the same session rebuilds onto this directory,
    # so its files have to survive the container.
    assert sandbox.workspace.path("note.txt").read_text() == "x"


def test_discard_workspace_refuses_anything_outside_its_root(tmp_path):
    (tmp_path / "root").mkdir()
    runner = DockerRunner(tmp_path / "root", MagicMock())
    outside = tmp_path / "kept"
    outside.mkdir()
    assert runner.discard_workspace(outside) is False
    assert outside.is_dir()
    # The managed root itself is never a valid target.
    assert runner.discard_workspace(runner.root) is False
    assert runner.root.is_dir()


async def test_sweep_removes_exited_containers_and_stale_directories(tmp_path):
    import os
    client = MagicMock()
    dead, alive = MagicMock(), MagicMock()
    dead.status, dead.attrs = "exited", {"Mounts": []}
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    os.utime(mounted, (0, 0))  # old, so only the mount keeps it alive
    alive.status = "running"
    alive.attrs = {"Mounts": [{"Source": str(mounted)}]}
    alive.labels = {}
    client.containers.list.return_value = [dead, alive]
    stale = tmp_path / "stale"
    stale.mkdir()
    os.utime(stale, (0, 0))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    retained = tmp_path / "retained"
    retained.mkdir()
    os.utime(retained, (0, 0))

    removed = await DockerRunner(tmp_path, client).sweep(keep=[retained])

    assert removed["containers"] == 1 and removed["keptRunning"] == 1
    assert not stale.exists(), "an old unmounted directory is a leftover"
    assert mounted.is_dir(), "a directory a running container mounts is in use"
    assert retained.is_dir(), "the pool may rebuild a reaped session onto this directory"
    assert fresh.is_dir(), "a directory younger than the threshold may be mid-creation"
    dead.remove.assert_called_once_with(force=True)
    alive.remove.assert_not_called()


async def test_sweep_removes_containers_whose_owner_is_gone(tmp_path):
    """A killed backend leaves containers running; only the instance file says so."""
    import os
    client = MagicMock()
    mine, orphan, stranger = MagicMock(), MagicMock(), MagicMock()
    for container in (mine, orphan, stranger):
        container.status, container.attrs, container.labels = "running", {"Mounts": []}, {}
    runner = DockerRunner(tmp_path, client, instance="mine")
    runner.state_dir.mkdir(parents=True, exist_ok=True)
    # "mine" is this process; "stranger" is another live backend; "orphan" is dead.
    (runner.state_dir / "stranger.json").write_text(json.dumps({"pid": os.getpid()}))
    (runner.state_dir / "orphan.json").write_text(json.dumps({"pid": 999999}))
    mine.labels = {"agentforge.instance": "mine"}
    orphan.labels = {"agentforge.instance": "orphan"}
    stranger.labels = {"agentforge.instance": "stranger"}
    client.containers.list.return_value = [mine, orphan, stranger]

    removed = await runner.sweep()

    assert removed["containers"] == 1 and removed["keptRunning"] == 2
    orphan.remove.assert_called_once_with(force=True)
    mine.remove.assert_not_called()
    stranger.remove.assert_not_called()
    assert not (runner.state_dir / "orphan.json").exists(), "a dead instance file is dropped"
    assert (runner.state_dir / "stranger.json").exists(), "a live backend keeps its claim"
