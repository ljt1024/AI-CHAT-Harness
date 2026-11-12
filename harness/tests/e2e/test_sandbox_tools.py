import docker
from docker.errors import DockerException
import pytest
from agentforge.sandbox.docker_runner import DockerRunner
from agentforge.sandbox.tools import SandboxTool

async def test_isolated_file_and_command_tools(tmp_path):
    try:
        client = docker.from_env(timeout=5)
        client.ping()
        client.images.get("python:3.12-slim")
    except DockerException as exc:
        pytest.skip(f"Real Docker requires python:3.12-slim: {type(exc).__name__}")
    runner = DockerRunner(tmp_path, client)
    box = await runner.create()
    try:
        write = SandboxTool(runner, box, 'write_file')
        assert write.permission == 'rule'
        assert (await write.execute({'path': 'result.txt', 'content': 'verified'})).ok
        read = SandboxTool(runner, box, 'read_file')
        assert (await read.execute({'path': 'result.txt'})).output == 'verified'
        assert (await write.execute({'path': 'result.txt', 'content': '-chunk', 'append': True})).ok
        assert (await read.execute({'path': 'result.txt'})).output == 'verified-chunk'
        content = '技能说明' * 700
        assert (await write.execute({'path': 'long.txt', 'content': content})).ok
        page = await read.execute({'path': 'long.txt', 'limit': 4000})
        assert page.ok and page.output.startswith(content[:1200])
        assert 'next_offset=1200' in page.output
        page = await read.execute({'path': 'long.txt', 'offset': 2400})
        assert page.ok and page.output.startswith(content[2400:]) and '[EOF.' in page.output
        assert not (await read.execute({'path': '/etc/passwd'})).ok
        command = SandboxTool(runner, box, 'run_command')
        assert (await command.execute({'command': 'pwd'})).output.strip() == '/workspace'
        assert not (await command.execute({'command': 'touch /forbidden'})).ok
    finally:
        await runner.destroy(box)
        runner.client.close()
