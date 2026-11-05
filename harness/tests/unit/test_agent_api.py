"""Agent definitions over the HTTP surface the creation wizard uses.

The wizard is only as honest as these endpoints: a definition it can save but
not run, a row it can edit across accounts, or a tool list that disagrees with
the registry would all show up here first.
"""

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done
from agentforge.gateway.registry import ModelGateway
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD

SECOND_USER = "second-user"


class Answers:
    async def chat_stream(self, req):
        yield ContentDelta(text="ok")
        yield Done()


@pytest.fixture
async def service(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    # The create path refuses an unregistered provider, so the fixture registers
    # the one the browser offers.
    gateway.register("deepseek", Answers())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD,
                              agentforge_workspace=tmp_path, sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        await app.state.auth.create_user(SECOND_USER, TEST_PASSWORD)
        yield app, store


def client_for(app):
    return AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def login_as(client, username):
    return client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})


def definition(**overrides):
    return {"name": "写作助手", "description": "把要点写成短文。",
            "system_prompt": "You write short pieces.", "tools": ["read_file"], **overrides}


async def test_tools_catalog_matches_the_registry(service):
    app, _ = service
    async with client_for(app) as client:
        payload = (await client.get("/api/tools")).json()["data"]
    names = {tool["name"] for tool in payload}
    assert {"read_file", "write_file", "grep", "glob", "run_command", "ask_user"} <= names
    # Skill helpers are injected into every agent by prepare_agent, so offering
    # them here would suggest a choice that does not exist.
    assert not names & {"list_skills", "load_skill", "install_skill", "delete_skill"}
    write = next(tool for tool in payload if tool["name"] == "write_file")
    assert write["permission"] == "rule" and write["description"]


async def test_create_then_list_round_trip(service):
    app, _ = service
    async with client_for(app) as client:
        created = await client.post("/api/agents", json=definition())
        assert created.status_code == 201, created.text
        row = created.json()["data"]
        assert row["description"] == "把要点写成短文。"
        listed = (await client.get("/api/agents")).json()["data"]
    stored = next(agent for agent in listed if agent["id"] == row["id"])
    # The list is what the picker renders: it has to carry the camelCase twins
    # the browser reads, not only the stored snake_case fields.
    assert stored["name"] == "写作助手"
    assert stored["systemPrompt"] == "You write short pieces."
    assert stored["modelId"] == "deepseek-chat"


async def test_unknown_tool_is_rejected_before_it_is_stored(service):
    app, _ = service
    async with client_for(app) as client:
        response = await client.post("/api/agents", json=definition(tools=["read_file", "drop_tables"]))
        assert response.status_code == 422
        assert "drop_tables" in response.json()["detail"]
        assert [a["id"] for a in (await client.get("/api/agents")).json()["data"]] == ["default"]


async def test_reserved_id_is_refused(service):
    app, _ = service
    async with client_for(app) as client:
        assert (await client.post("/api/agents", json=definition(id="default"))).status_code == 409


async def test_update_replaces_the_definition_and_follows_the_path(service):
    app, store = service
    async with client_for(app) as client:
        agent_id = (await client.post("/api/agents", json=definition())).json()["data"]["id"]
        # A body naming another id must not move the row: the path owns identity.
        updated = await client.put(f"/api/agents/{agent_id}",
                                   json=definition(name="编辑后的助手", id="somewhere-else"))
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["id"] == agent_id
        rows = await store.db.agents.find({"name": "编辑后的助手"}).to_list(None)
        assert [row["id"] for row in rows] == [agent_id]
        assert (await store.db.agents.find({"id": "somewhere-else"}).to_list(None)) == []


async def test_update_of_an_unknown_agent_is_404(service):
    app, _ = service
    async with client_for(app) as client:
        assert (await client.put("/api/agents/missing", json=definition())).status_code == 404


async def test_builtin_agent_is_shared_and_read_only(service):
    app, _ = service
    async with client_for(app) as client:
        assert (await client.put("/api/agents/default", json=definition())).status_code == 409
        assert (await client.delete("/api/agents/default")).status_code == 409
        still_there = (await client.get("/api/agents")).json()["data"]
    assert [agent["id"] for agent in still_there] == ["default"]


async def test_another_account_neither_sees_nor_edits_it(service):
    app, _ = service
    async with client_for(app) as first:
        agent_id = (await first.post("/api/agents", json=definition())).json()["data"]["id"]
        async with client_for(app) as second:
            assert (await login_as(second, SECOND_USER)).status_code == 200
            assert agent_id not in [a["id"] for a in (await second.get("/api/agents")).json()["data"]]
            # 404 rather than 403: answering 403 would confirm the row exists.
            assert (await second.put(f"/api/agents/{agent_id}", json=definition(name="偷改"))).status_code == 404
            assert (await second.delete(f"/api/agents/{agent_id}")).status_code == 404
        still_mine = [a["id"] for a in (await first.get("/api/agents")).json()["data"]]
    assert agent_id in still_mine


async def test_delete_removes_the_row(service):
    app, store = service
    async with client_for(app) as client:
        agent_id = (await client.post("/api/agents", json=definition())).json()["data"]["id"]
        assert (await client.delete(f"/api/agents/{agent_id}")).status_code == 200
        assert [a["id"] for a in (await client.get("/api/agents")).json()["data"]] == ["default"]
        assert (await client.delete(f"/api/agents/{agent_id}")).status_code == 404
    assert await store.db.agents.count_documents({"id": agent_id}) == 0


async def test_the_same_mcp_server_can_back_more_than_one_agent(service, mcp_service):
    """Two agents sharing a server must both save.

    Validation used to reuse one cached registry and register the server's tools
    into it, so the second save died on "Duplicate tool" -- a failure the wizard
    would show as an opaque 422 on an otherwise valid definition.
    """
    app, _ = service
    url, _workspace = mcp_service
    async with client_for(app) as client:
        registered = await client.post("/api/mcp/servers", json={"id": "local", "url": url})
        assert registered.status_code == 201, registered.text
        for name in ("第一个助手", "第二个助手"):
            created = await client.post("/api/agents", json=definition(name=name, mcp_servers=["local"]))
            assert created.status_code == 201, created.text


class StubStore:
    def __init__(self, rows):
        self.rows = rows

    async def list(self, collection, *args, **kwargs):
        return self.rows


async def test_a_duplicated_server_row_is_attached_once():
    """A shared legacy row plus its owned twin must not attach twice.

    That is what made every MCP-backed agent unsavable: each attach registers the
    server's tools, and the second registration raises "Duplicate tool".
    """
    from agentforge.capabilities import run_servers

    rows = [{"id": "fetch", "enabled": True, "owner_id": ""},
            {"id": "fetch", "enabled": True, "owner_id": "user-1"},
            {"id": "jdselection", "enabled": False, "owner_id": "user-1"}]
    ids, required = await run_servers(StubStore(rows), [], True)
    assert ids == ["fetch"]
    assert required == set()


async def test_a_visitor_may_browse_but_not_build(service):
    app, _ = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as guest:
        listed = await guest.get("/api/agents")
        assert listed.status_code == 200 and [a["id"] for a in listed.json()["data"]] == ["default"]
        # Building requires an account: the wizard answers 401 and the interface
        # opens the sign-in dialog instead of silently writing a guest-owned row.
        assert (await guest.post("/api/agents", json=definition())).status_code == 401
        assert (await guest.get("/api/tools")).status_code == 401
