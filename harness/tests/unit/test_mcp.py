import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys

from agentforge.mcp_client import discover_tools
from agentforge.tools.base import ToolRegistry


async def test_real_mcp_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Path(__file__).resolve().parents[3] / "mcp-server/src/main.py"
    process = subprocess.Popen([sys.executable, str(server)], env={
        "PATH": os.defpath, "MCP_PORT": str(port), "MCP_WORKSPACE": str(tmp_path)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError("MCP server exited")
                await asyncio.sleep(0.05)
        tools = await discover_tools(f"http://127.0.0.1:{port}/mcp")
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        assert len(registry.schemas(["read_file", "write_file"])) == 2
        assert (await registry.execute("write_file", {"path": "test.txt", "content": "mcp"})).ok
        assert (await registry.execute("read_file", {"path": "test.txt"})).output == "mcp"
        assert not (await registry.execute("read_file", {"path": "../outside"})).ok
    finally:
        process.terminate()
        process.wait(timeout=5)
