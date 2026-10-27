import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentforge.capabilities import MCPServerRequest, parse_config, public_server
from agentforge.mcp_client import MCPTool, discover_tools
from agentforge.redact import describe, mask_url, status_code

# The shape a ModelScope deployment returns: the tenant id lives in the path.
HOSTED = "https://mcp.api-inference.modelscope.net/bf56fce2e65e40/streamable_http"
# What the ModelScope console tells you to paste, verbatim.
PASTED = {
    "mcpServers": {
        "fetch": {
            "type": "streamable_http",
            "url": "https://mcp.api-inference.modelscope.net/06be4f9db2ed44/mcp",
            "headers": {"Authorization": "Bearer ms-0123456789abcdef"},
        }
    }
}
PASTED_URL = PASTED["mcpServers"]["fetch"]["url"]



def test_mask_url_hides_the_tenant_segment():
    assert mask_url(HOSTED) == "https://mcp.api-inference.modelscope.net/****/streamable_http"
    # A self-hosted endpoint has nothing to hide and stays readable.
    assert mask_url("http://127.0.0.1:9000/mcp") == "http://127.0.0.1:9000/mcp"


def test_public_server_never_carries_the_token_or_the_raw_url():
    server = {"id": "amap", "url": HOSTED, "token": "ms-secret", "tools": [{"name": "mcp_amap__x"}]}
    public = public_server(server)
    assert public["hasToken"] is True
    assert "token" not in public
    assert "bf56fce2e65e40" not in public["url"]


def test_token_is_trimmed_and_unusable_values_are_rejected():
    assert MCPServerRequest(id="amap", url=HOSTED, token="  ms-abcdef  ").token == "ms-abcdef"
    # An empty field means "this deployment needs no token", not "send ''".
    assert MCPServerRequest(id="amap", url=HOSTED, token="   ").token is None
    with pytest.raises(ValidationError):
        MCPServerRequest(id="amap", url=HOSTED, token="has a space")


def test_the_pasted_catalogue_block_becomes_one_server():
    request = parse_config(json.dumps(PASTED))
    assert (request.id, request.url, request.token) == (
        "fetch", PASTED_URL, "ms-0123456789abcdef")
    # headers is an input format, not something the runtime replays.
    assert request.headers is None and request.type == "streamable_http"


@pytest.mark.parametrize("config, message", [
    ("{}", "needs a url"),
    ('{"mcpServers": {}}', "non-empty"),
    ('{"mcpServers": {"a": {"url": "URL"}, "b": {"url": "URL"}}}', "one at a time"),
    ('{"mcpServers": {"a": {"command": "npx"}}}', "stdio"),
    ('{"mcpServers": {"a": {"url": "URL", "headers": {"X-Api-Key": "k"}}}}', "Only an Authorization"),
    ('{"mcpServers": {"a": {"url": "URL", "headers": {"Authorization": "ms-01234567"}}}}', "Bearer"),
    ('{"mcpServers": {"a": {"url": "URL", "type": "sse"}}}', "Streamable HTTP"),
    ("not json", "Not valid JSON"),
])
def test_an_unusable_pasted_config_says_why(config, message):
    with pytest.raises(ValueError, match=message):
        parse_config(config.replace("URL", PASTED_URL))


def test_a_bare_server_object_still_gets_a_usable_id():
    assert parse_config(json.dumps({"url": PASTED_URL})).id == "mcp"
    assert parse_config(json.dumps({"mcpServers": {"My Fetch": {"url": PASTED_URL}}})).id == "my_fetch"


def test_a_rejected_config_reports_one_readable_sentence():
    """The UI prints this verbatim, so pydantic's report must not leak through."""
    with pytest.raises(ValueError) as caught:
        parse_config(json.dumps({"mcpServers": {"a": {"url": "ftp://host/mcp"}}}))
    message = str(caught.value)
    assert message == "Use an HTTP(S) MCP URL without credentials, query or fragment."


def test_describe_unwraps_the_group_and_masks_the_url_inside_it():
    """An async client wraps the useful error, and that error quotes the URL."""
    inner = RuntimeError(f"Client error '401 Unauthorized' for url '{HOSTED}'")
    text = describe(ExceptionGroup("unhandled errors in a TaskGroup", [inner]))
    assert "401 Unauthorized" in text and text.startswith("RuntimeError:")
    assert "bf56fce2e65e40" not in text and "****" in text


def test_status_code_reads_through_the_group():
    class Response:
        status_code = 401

    class Rejected(Exception):
        response = Response()

    assert status_code(ExceptionGroup("group", [Rejected("no")])) == 401


async def test_import_endpoint_stores_the_token_and_never_returns_it(tmp_path, monkeypatch):
    """Pasting twice refreshes the row: the URL identifies the service."""
    import httpx
    from mongomock_motor import AsyncMongoMockClient

    from agentforge.api import create_app
    from agentforge.config import Settings
    from agentforge.store import MongoDBStore

    async def fake_discover(url, token=None, timeout=None):
        assert (url, token) == (PASTED_URL, "ms-0123456789abcdef")
        return [MCPTool(url, SimpleNamespace(name="fetch", description="Fetch a page",
                                             inputSchema={"type": "object"}), token)]

    monkeypatch.setattr("agentforge.capabilities.discover_tools", fake_discover)
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path, sandbox_warm_size=0), store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            created = await client.post("/api/mcp/servers/import",
                                        json={"config": json.dumps(PASTED)})
            again = await client.post("/api/mcp/servers/import",
                                      json={"config": json.dumps(PASTED)})
            rejected = await client.post("/api/mcp/servers/import", json={"config": "{}"})
            listed = await client.get("/api/mcp/servers")
    assert created.status_code == 201 and again.status_code == 201
    assert [server["id"] for server in listed.json()["data"]] == ["fetch"]
    server = created.json()["data"]
    assert [tool["name"] for tool in server["tools"]] == ["mcp_fetch__fetch"]
    assert server["url"].endswith("/****/mcp") and server["hasToken"] is True
    assert "ms-0123456789abcdef" not in listed.text and "06be4f9db2ed44" not in listed.text
    assert rejected.status_code == 422 and "url" in rejected.json()["detail"]


async def test_listing_hides_the_credential_from_the_browser(tmp_path):
    """Which row a user sees is not a secret; the token and the tenant id are."""
    import httpx
    from mongomock_motor import AsyncMongoMockClient

    from agentforge.api import create_app
    from agentforge.config import Settings
    from agentforge.store import MongoDBStore

    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    await store.put("mcp_servers", {"id": "amap", "url": HOSTED, "token": "ms-secret", "tools": []})
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path, sandbox_warm_size=0), store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api/mcp/servers")
    assert response.status_code == 200
    [server] = response.json()["data"]
    assert server["id"] == "amap" and server["hasToken"] is True
    assert "ms-secret" not in response.text and "bf56fce2e65e40" not in response.text


async def test_discovery_needs_the_token_when_the_service_demands_one(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Path(__file__).resolve().parents[3] / "mcp-server/src/main.py"
    process = subprocess.Popen([sys.executable, str(server)], env={
        "PATH": os.defpath, "MCP_PORT": str(port), "MCP_WORKSPACE": str(tmp_path),
        "MCP_REQUIRE_TOKEN": "demo-token"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
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
        # A deployment made with auth_check rejects the handshake, so a missing
        # or wrong token must fail rather than fall back to anonymous access.
        with pytest.raises(Exception):
            await discover_tools(url, None, 10)
        with pytest.raises(Exception):
            await discover_tools(url, "wrong-token", 10)
        tools = await discover_tools(url, "demo-token", 10)
        assert {"read_file", "write_file"} <= {tool.name for tool in tools}
        assert tools[0].token == "demo-token"
    finally:
        process.terminate()
        process.wait(timeout=5)
