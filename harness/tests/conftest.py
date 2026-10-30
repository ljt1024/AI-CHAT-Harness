import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys

import docker
from docker.errors import DockerException
import pytest

# `helpers.py` lives beside this file, and importlib mode does not put the tests
# directory on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Tests must never read the developer's real MCP startup file: it holds bearer
# tokens, and every entry it lists costs a discovery round trip each time a
# lifespan starts, which would make the suite depend on a hosted service.
os.environ.setdefault("MCP_CONFIG", str(Path(__file__).resolve().parent / "no-mcp.json"))


@pytest.fixture
async def mcp_service(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    workspace = tmp_path / "external-mcp"
    server = Path(__file__).resolve().parents[2] / "mcp-server/src/main.py"
    process = subprocess.Popen([sys.executable, str(server)], env={
        "PATH": os.defpath, "MCP_PORT": str(port), "MCP_WORKSPACE": str(workspace)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                assert process.poll() is None, "MCP server exited"
                await asyncio.sleep(0.05)
        else:
            pytest.fail("MCP server did not start")
        yield f"http://127.0.0.1:{port}/mcp", workspace
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture
def docker_client():
    client = None
    try:
        client = docker.from_env(timeout=5)
        client.ping()
        client.images.get("python:3.12-slim")
    except DockerException as exc:
        if client:
            client.close()
        pytest.skip(f"Real Docker requires python:3.12-slim: {type(exc).__name__}")
    try:
        yield client
    finally:
        client.close()
