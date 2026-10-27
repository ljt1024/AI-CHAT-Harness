from contextlib import asynccontextmanager
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from agentforge.capabilities import MCPServerRequest, server_tools
from agentforge.mcp_client import MCPTool, discover_tools


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.org", "http://u:p@host/mcp",
                                 "https://host/mcp?token=private", "http://host/mcp#fragment"])
def test_mcp_url_rejects_unsupported_or_embedded_secrets(url):
    with pytest.raises(ValidationError):
        MCPServerRequest(id="local", url=url)


async def test_namespaced_mcp_tools_always_require_approval(mcp_service):
    url, _ = mcp_service
    tools = await server_tools({"id": "local", "url": url})
    assert all(tool.name.startswith("mcp_local__") for tool in tools)
    assert all(tool.permission == "confirm" for tool in tools)
    assert next(tool for tool in tools if tool.name.endswith("__read_file")).remote_name == "read_file"


async def test_mcp_failure_returns_actionable_tool_error():
    tool = MCPTool("http://127.0.0.1:1/mcp", SimpleNamespace(
        name="remote", description="Test", inputSchema={"type": "object"}))
    result = await tool.execute({})
    assert not result.ok
    assert "verify its state" in result.error
    assert "127.0.0.1" not in result.error


@pytest.mark.parametrize("repeated", [False, True])
async def test_mcp_discovery_paginates_and_detects_repeated_cursors(monkeypatch, repeated):
    cursors = []

    class Session:
        async def list_tools(self, cursor=None):
            cursors.append(cursor)
            definition = SimpleNamespace(name=f"tool{len(cursors)}", description="Test", inputSchema={})
            return SimpleNamespace(tools=[definition], nextCursor="page2" if cursor is None or repeated else None)

    @asynccontextmanager
    async def fake_connect(url, token=None, timeout=None):
        yield Session()

    monkeypatch.setattr("agentforge.mcp_client.connect", fake_connect)
    if repeated:
        with pytest.raises(ValueError, match="repeated"):
            await discover_tools("http://test/mcp")
    else:
        tools = await discover_tools("http://test/mcp")
        assert len(tools) == 2
        assert cursors == [None, "page2"]
