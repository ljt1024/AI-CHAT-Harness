"""Two accounts, one process: what each of them can and cannot reach.

Every assertion here is made over HTTP through the real cookie path, so a bug in
the middleware, the ownership filter or the SSE gate shows up as a failed test
rather than as a quiet leak.
"""

import asyncio

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD

SECOND_USER = "second-user"


class CallsCommand:
    """Asks for a run_command, which the agent's policy parks on approval."""

    async def chat_stream(self, req):
        if any(message["role"] == "tool" for message in req.messages):
            yield ContentDelta(text="Done.")
        else:
            yield ToolCallRequest(call=ToolCall(name="run_command",
                                                arguments={"command": "echo hello"}))
        yield UsageReport(tokens_in=10, tokens_out=5, usd=0.00001)
        yield Done()


class Answers:
    async def chat_stream(self, req):
        yield ContentDelta(text="A short answer.")
        yield UsageReport(tokens_in=10, tokens_out=5, usd=0.00001)
        yield Done()


@pytest.fixture
async def service(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", CallsCommand())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path, sandbox_warm_size=0)
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        await app.state.auth.create_user(SECOND_USER, TEST_PASSWORD)
        yield app, store, settings


def client_for(app):
    return AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def anonymous(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def login(client, username):
    response = await client.post("/api/auth/login",
                                 json={"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    client.cookies.clear()
    client.cookies.set("agentforge_session",
                       response.cookies.get("agentforge_session"), domain="test.local")
    return response.json()["data"]


# Routes a visitor may reach without an account. Everything else answers 401.
TRIAL = [
    ("GET", "/api/agents"),
    ("GET", "/api/models"),
    ("GET", "/api/interventions/policy"),
    ("GET", "/api/approvals"),
]

# Changing the installation is not part of a trial: these stay behind the wall.
ACCOUNT_ONLY = [
    ("POST", "/api/agents"),
    ("GET", "/api/sessions"),
    ("GET", "/api/skills"),
    ("POST", "/api/skills"),
    ("GET", "/api/mcp/servers"),
    ("GET", "/api/traces/anything"),
    ("POST", "/api/sandbox/exec"),
    ("DELETE", "/api/sessions/anything"),
]


async def test_anonymous_requests_reach_only_the_trial_routes(service):
    app, _, _ = service
    async with anonymous(app) as client:
        for method, path in ACCOUNT_ONLY:
            response = await client.request(method, path, json={})
            assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        for method, path in TRIAL:
            response = await client.request(method, path, json={})
            assert response.status_code == 200, f"{method} {path} -> {response.status_code}"
        assert (await client.get("/api/auth/me")).json()["data"] is None


async def test_login_logout_and_cookie_flags(service):
    app, _, _ = service
    async with anonymous(app) as client:
        response = await client.post("/api/auth/login",
                                     json={"username": "admin", "password": TEST_PASSWORD})
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        assert "secure" not in cookie.lower(), "plain-HTTP development must still work"
        assert (await client.get("/api/auth/me")).json()["data"]["username"] == "admin"
        assert (await client.post("/api/auth/logout")).status_code == 200
        # Signing out returns the caller to the trial, not to a dead page.
        assert (await client.get("/api/skills")).status_code == 401
        me = (await client.get("/api/auth/me")).json()
        assert me["data"] is None
        assert me["guest"]["turnsLeft"] == 4


async def test_a_wrong_password_is_rejected_without_a_cookie(service):
    app, _, _ = service
    async with anonymous(app) as client:
        response = await client.post("/api/auth/login",
                                     json={"username": "admin", "password": "definitely-wrong"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


async def test_agents_sessions_and_traces_do_not_cross_accounts(service):
    app, _, _ = service
    async with client_for(app) as first:
        created = await first.post("/api/agents", json={"name": "Mine", "tools": []})
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]
        started = await first.post("/api/sessions", json={"agent_id": agent_id,
                                                          "task": "Say something"})
        assert started.status_code == 201
        session_id = started.json()["session_id"]
        for _ in range(100):
            if app.state.runs.get(session_id) is None:
                break
            await asyncio.sleep(0.02)

        async with client_for(app) as second:
            await login(second, SECOND_USER)
            listed = await second.get("/api/agents")
            assert agent_id not in [agent["id"] for agent in listed.json()["data"]]
            assert (await second.get(f"/api/traces/{session_id}")).status_code == 404
            assert (await second.get(f"/api/sessions/{session_id}/context")).status_code == 404
            assert (await second.delete(f"/api/sessions/{session_id}")).status_code == 404
            assert (await second.post(f"/api/sessions/{session_id}/stop")).status_code == 404
            assert (await second.get(f"/api/sessions/{session_id}/events")).status_code == 404
            # Keeping the same id must not let B continue A's conversation.
            hijack = await second.post("/api/agents/run", json={
                "input": "continue please", "sessionId": session_id, "stream": False})
            assert hijack.status_code == 404
            # And A's agent id is not a handle B can run either.
            borrowed = await second.post("/api/sessions", json={"agent_id": agent_id,
                                                                "task": "run it"})
            assert borrowed.status_code == 404

        assert (await first.get(f"/api/traces/{session_id}")).status_code == 200


async def test_the_same_skill_content_installs_separately(service):
    app, store, _ = service
    package = {"name": "shared-demo", "files": {"SKILL.md": "# Shared\n\nSame bytes."}}
    async with client_for(app) as first:
        assert (await first.post("/api/skills", json=package)).status_code == 201
        async with client_for(app) as second:
            await login(second, SECOND_USER)
            assert (await second.get("/api/skills")).json()["data"] == []
            assert (await second.post("/api/skills", json=package)).status_code == 201
            mine = (await first.get("/api/skills")).json()["data"]
            theirs = (await second.get("/api/skills")).json()["data"]
    assert len(mine) == 1 and len(theirs) == 1
    assert mine[0]["id"] == theirs[0]["id"], "the id is content-derived, the row is per owner"
    rows = await store.db.skills.count_documents({"id": mine[0]["id"]})
    assert rows == 2, "two rows, one per account, both unique within their owner"
    async with client_for(app) as second:
        await login(second, SECOND_USER)
        assert (await second.delete(f"/api/skills/{theirs[0]['id']}")).status_code == 200
        async with client_for(app) as first:
            assert len((await first.get("/api/skills")).json()["data"]) == 1, \
                "B deleting its copy must leave A's alone"


async def test_an_approval_cannot_be_resolved_by_another_account(service):
    app, _, _ = service
    async with client_for(app) as first:
        created = await first.post("/api/agents", json={
            "name": "Needs approval", "tools": ["run_command"],
            "permissions": {"run_command": "confirm"}})
        agent_id = created.json()["data"]["id"]
        started = await first.post("/api/sessions", json={"agent_id": agent_id,
                                                          "task": "run it"})
        assert started.status_code == 201
        for _ in range(200):
            if app.state.approvals.pending:
                break
            await asyncio.sleep(0.02)
        assert app.state.approvals.pending, "the command should be waiting for a human"
        call_id = next(iter(app.state.approvals.pending))

        async with client_for(app) as second:
            await login(second, SECOND_USER)
            assert (await second.get("/api/approvals")).json()["data"] == []
            denied = await second.post(f"/api/approvals/{call_id}", json={"approved": True})
            assert denied.status_code == 404, "B must not be able to approve A's command"
        assert call_id in app.state.approvals.pending, "the request is still pending"
        assert (await first.post(f"/api/approvals/{call_id}",
                                 json={"approved": False})).status_code == 200
        assert (await first.post(f"/api/approvals/{call_id}",
                                 json={"approved": True})).status_code == 409


async def test_an_artifact_is_downloadable_only_by_its_owner(service):
    app, store, _ = service
    async with client_for(app) as first:
        owner = (await first.get("/api/auth/me")).json()["data"]["id"]
        await store.put("sessions", {"id": "theirs"}, owner=owner)
        await store.put("artifacts", {"id": "file-1", "sessionId": "theirs",
            "artifact": {"fileId": "file-1", "fileName": "note.txt", "format": "txt",
                         "mimeType": "text/plain", "size": 5},
            "content": b"hello", "sha256": "x"}, owner=owner)
        assert (await first.get("/api/files/file-1/download")).status_code == 200
        async with client_for(app) as second:
            await login(second, SECOND_USER)
            assert (await second.get("/api/files/file-1/download")).status_code == 404


async def test_a_cross_origin_write_is_refused(service):
    app, _, _ = service
    async with client_for(app) as client:
        response = await client.post("/api/agents", json={"name": "Cross", "tools": []},
                                     headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        same = await client.post("/api/agents", json={"name": "Same", "tools": []},
                                 headers={"Origin": "http://test"})
        assert same.status_code == 201


async def test_disabling_an_account_ends_its_sessions(service):
    app, _, _ = service
    async with client_for(app) as client:
        user = (await client.get("/api/auth/me")).json()["data"]
        assert (await client.get("/api/agents")).status_code == 200
        await app.state.auth.revoke_user(user["id"])
        # The revoked session drops the caller to the anonymous trial; what it
        # must never do is keep answering as the account.
        assert (await client.get("/api/skills")).status_code == 401
        assert (await client.get("/api/auth/me")).json()["data"] is None
