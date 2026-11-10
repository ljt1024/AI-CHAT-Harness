"""The service catalogue, the address guard, and credentials at rest.

Three properties are tested here because they only matter together: a user may
install a server without ever typing a URL, registration refuses to dial the
host's own network, and the credential that gets stored is not readable from the
database.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from agentforge import netguard, secrets_box
from agentforge.api import create_app
from agentforge.catalog import parse_catalog
from agentforge.config import Settings
from agentforge.mcp_client import MCPTool
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD

CATALOG = {"servers": {
    "Team Gateway": {
        "name": "团队工具网关",
        "summary": "内部服务，所有成员可用",
        "url": "https://gateway.example.com/mcp",
        "auth": "token",
        "docs": "https://gateway.example.com/tokens",
        "verified": True,
    },
    "Public Docs": {
        "name": "公开文档服务",
        "url": "https://docs.example.com/mcp",
        "auth": "none",
    },
}}


@pytest.fixture(autouse=True)
def no_external_dns(monkeypatch):
    """Registration resolves the host before dialling it. A literal address is
    still classified for real, which is what the guard tests look at."""
    async def fake(host):
        return [host] if host.replace(".", "").isdigit() else ["93.184.216.34"]
    monkeypatch.setattr("agentforge.netguard.resolve", fake)


def build(tmp_path, catalog=True, **overrides):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    path = tmp_path / "catalog.json"
    if catalog:
        path.write_text(json.dumps(CATALOG), encoding="utf-8")
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path, sandbox_warm_size=0,
                        mcp_config=tmp_path / "no-mcp.json", mcp_catalog=path, **overrides)
    return store, create_app(settings, store)


def client_for(app):
    return AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_guard_refuses_addresses_that_belong_to_this_host():
    for url in ("http://127.0.0.1:9000/mcp", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/mcp", "http://192.168.1.10:8000/mcp"):
        with pytest.raises(netguard.BlockedAddress):
            await netguard.assert_public(url)


async def test_loopback_is_reachable_only_when_the_operator_says_so():
    assert await netguard.assert_public("http://127.0.0.1:9000/mcp", allow_private=True) is True
    # Link-local is where the metadata endpoint lives; no sidecar is addressed there.
    with pytest.raises(netguard.BlockedAddress):
        await netguard.assert_public("http://169.254.169.254/", allow_private=True)


async def test_a_pasted_url_cannot_aim_this_host_at_its_own_network(tmp_path):
    """The panel is a front door now, so its URL field is a request to use the
    host's network position unless something refuses it."""
    _, app = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            refused = await client.post("/api/mcp/servers",
                                        json={"id": "local", "url": "http://127.0.0.1:27017/mcp"})
            registered = await client.get("/api/mcp/servers")
    assert refused.status_code == 422
    assert "does not dial that address" in refused.json()["detail"]
    assert registered.json()["data"] == []


async def test_the_page_installs_a_server_without_ever_seeing_its_url(tmp_path, monkeypatch):
    async def fake_discover(url, token=None, timeout=None):
        assert (url, token) == ("https://gateway.example.com/mcp", "team-token")
        return [MCPTool(url, SimpleNamespace(name="lookup", description="Look up a record",
                                            inputSchema={"type": "object"}), token)]

    monkeypatch.setattr("agentforge.capabilities.discover_tools", fake_discover)
    _, app = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            listed = (await client.get("/api/mcp/catalog")).json()["data"]
            installed = await client.post("/api/mcp/catalog/team_gateway/install",
                                          json={"token": "team-token"})
            twice = await client.post("/api/mcp/catalog/team_gateway/install",
                                      json={"token": "team-token"})
            after = (await client.get("/api/mcp/catalog")).json()["data"]
    assert {item["id"] for item in listed} == {"team_gateway", "public_docs"}
    assert {item["id"]: item["installed"] for item in listed} == {
        "team_gateway": False, "public_docs": False}
    assert installed.status_code == 201
    assert [tool["name"] for tool in installed.json()["data"]["tools"]] == ["mcp_team_gateway__lookup"]
    assert twice.status_code == 409
    assert {item["id"]: item["installed"] for item in after}["team_gateway"] is True


async def test_installing_asks_for_the_credential_the_entry_declares(tmp_path):
    _, app = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            missing = await client.post("/api/mcp/catalog/team_gateway/install", json={})
            unknown = await client.post("/api/mcp/catalog/nope/install", json={})
    assert missing.status_code == 422 and "access token" in missing.json()["detail"]
    assert unknown.status_code == 404


async def test_a_token_is_ciphertext_at_rest_and_plaintext_to_the_client(tmp_path, monkeypatch):
    seen = {}

    async def fake_discover(url, token=None, timeout=None):
        seen["token"] = token
        return []

    monkeypatch.setattr("agentforge.capabilities.discover_tools", fake_discover)
    store, app = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await client.post("/api/mcp/catalog/team_gateway/install", json={"token": "team-token"})
    row = await store.db.mcp_servers.find_one({"id": "team_gateway"})
    assert row["token"].startswith("enc:v1:")
    assert "team-token" not in row["token"]
    assert seen["token"] == "team-token"
    assert secrets_box.decrypt(row["token"]) == "team-token"


async def test_a_plaintext_row_from_before_is_encrypted_on_the_next_start(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    await store.put("mcp_servers", {"id": "legacy", "url": "https://legacy.example.com/mcp",
                                    "token": "old-plain", "tools": []})
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path, sandbox_warm_size=0,
                        mcp_config=tmp_path / "no-mcp.json", mcp_catalog=tmp_path / "no-catalog.json")
    app = create_app(settings, store)
    async with app.router.lifespan_context(app):
        pass
    row = await store.db.mcp_servers.find_one({"id": "legacy"})
    assert row["token"].startswith("enc:v1:")
    assert secrets_box.decrypt(row["token"]) == "old-plain"


def test_a_broken_entry_costs_one_row_not_the_catalogue(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"servers": {
        "good": {"name": "Good", "url": "https://good.example.com/mcp"},
        "bad": {"name": "Bad", "url": "ftp://bad.example.com/mcp"},
        "no_url": {"name": "No URL"},
    }}), encoding="utf-8")
    assert [item.id for item in parse_catalog(path)] == ["good"]


def test_a_human_key_becomes_the_id_the_runtime_namespaces(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"servers": {
        "Company Gateway": {"name": "网关", "url": "https://g.example.com/mcp"}}}), encoding="utf-8")
    assert [item.id for item in parse_catalog(path)] == ["company_gateway"]
