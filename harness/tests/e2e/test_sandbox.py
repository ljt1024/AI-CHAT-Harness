import json

import docker
from docker.errors import DockerException
import pytest

from agentforge.sandbox.docker_runner import DockerRunner


@pytest.fixture
async def sandbox(tmp_path):
    try:
        client = docker.from_env(timeout=5)
        client.ping()
        client.images.get("python:3.12-slim")
    except DockerException as exc:
        pytest.skip(f"Real Docker sandbox requires python:3.12-slim: {type(exc).__name__}")
    runner = DockerRunner(tmp_path, client)
    sandbox = await runner.create()
    try:
        yield runner, sandbox
    finally:
        await runner.destroy(sandbox)
        client.close()


async def test_root_readonly_workspace_writable_and_no_credentials(sandbox):
    runner, box = sandbox
    assert (await runner.execute(box, ["sh", "-c", "echo ok > /workspace/result.txt"])).ok
    assert not (await runner.execute(box, ["sh", "-c", "echo fail > /root-write-test"])).ok
    result = await runner.execute(box, ["python", "-c", "import os,json; print(json.dumps(dict(os.environ)))"])
    env = json.loads(result.output)
    assert not any(part in key.upper() for key in env for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    assert not (await runner.execute(box, ["sh", "-c", "cat /proc/1/environ | grep -Ei 'API_KEY|TOKEN|SECRET'"])).ok


async def test_process_limit_and_egress_denied(sandbox):
    runner, box = sandbox
    code = """import os,time
children=[]
try:
    for _ in range(300):
        pid=os.fork()
        if pid==0:
            time.sleep(10)
            os._exit(0)
        children.append(pid)
    print('UNBOUNDED')
except OSError:
    print('PID_LIMIT_ENFORCED')
finally:
    for pid in children:
        os.kill(pid,9)
        os.waitpid(pid,0)
"""
    result = await runner.execute(box, ["python", "-c", code])
    assert result.ok and "PID_LIMIT_ENFORCED" in result.output
    network = await runner.execute(box, ["python", "-c",
        "import socket; socket.create_connection(('1.1.1.1',443),timeout=1)"])
    assert not network.ok


async def test_timeout_terminates_entire_container(sandbox):
    runner, box = sandbox
    result = await runner.execute(box, ["sleep", "60"], timeout=0.1)
    assert not result.ok and not box.alive
