"""Resources written before accounts existed.

`adopt_ownerless` binds them to the initial account, which is right for anything
a real session produced -- and wrong for the built-in agent, whose empty owner is
what makes it readable by every account and every visitor. Adopting it is
invisible to the account that gains it, and looks like "Agent not found." to
everybody else.
"""

import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.registry import ModelGateway
from agentforge.models import DEFAULT_AGENT_ID
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD


def build(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD,
                              agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, ModelGateway())
    return app, store


async def test_adoption_leaves_the_shared_agent_alone(tmp_path):
    app, store = build(tmp_path)
    await store.db.agents.insert_one({"id": DEFAULT_AGENT_ID, "name": "AgentForge"})
    await store.db.agents.insert_one({"id": "legacy-agent", "name": "Old"})
    await store.db.sessions.insert_one({"id": "legacy-session"})
    async with app.router.lifespan_context(app):
        shared = await store.db.agents.find_one({"id": DEFAULT_AGENT_ID})
        assert shared["owner_id"] == "", "the shared agent must stay unowned"
        adopted = await store.db.agents.find_one({"id": "legacy-agent"})
        session = await store.db.sessions.find_one({"id": "legacy-session"})
        assert adopted["owner_id"] and session["owner_id"] == adopted["owner_id"], \
            "everything else belongs to the account the operator designated"


async def test_the_agent_survives_adoption_for_everyone_else(tmp_path):
    """The regression: an unowned default row left the catalogue empty for guests."""
    app, store = build(tmp_path)
    await store.db.agents.insert_one({"id": DEFAULT_AGENT_ID, "name": "AgentForge"})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            catalogue = (await client.get("/api/agents")).json()["data"]
            assert [row["id"] for row in catalogue] == [DEFAULT_AGENT_ID]
            started = await client.post("/api/sessions",
                                        json={"agent_id": DEFAULT_AGENT_ID, "task": "hi"})
            assert started.status_code != 404, started.text


async def test_a_private_copy_left_by_an_earlier_migration_is_dropped(tmp_path):
    """The row this bug left behind: the built-in agent owned by one account.

    Keeping it would show two agents with the same id to that account and none
    to anybody else; the id is reserved, so no API path can legitimately own it.
    """
    app, store = build(tmp_path)
    await store.db.agents.insert_one({"id": DEFAULT_AGENT_ID, "name": "AgentForge",
                                      "owner_id": "an-account"})
    async with app.router.lifespan_context(app):
        rows = await store.db.agents.find({"id": DEFAULT_AGENT_ID}).to_list(length=10)
        assert [row["owner_id"] for row in rows] == [""], "one shared row, no private copy"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert len((await client.get("/api/agents")).json()["data"]) == 1
