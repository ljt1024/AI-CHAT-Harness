"""The anonymous trial: browse, try a few real turns, then sign in.

The wall sits where it costs the visitor something instead of at the front door,
so these tests pin both halves of that bargain: what a visitor may reach, and
what it takes away from the operator (turn budget, site budget, retention).
"""

import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.auth import GUEST_COOKIE, is_guest, new_guest_id, valid_guest_id
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD


class Answers:
    async def chat_stream(self, req):
        yield ContentDelta(text="A short answer.")
        yield UsageReport(tokens_in=10, tokens_out=5, usd=0.00001)
        yield Done()


def build(tmp_path, **overrides):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", Answers())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path, sandbox_warm_size=0, **overrides)
    return create_app(settings, store, gateway), store, settings


def visitor(app):
    """A browser-shaped client: it keeps whatever cookies the server sets."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def chat_body(text="hello"):
    return {"model": "deepseek-chat", "stream": True,
            "messages": [{"role": "user", "content": text}]}


async def test_a_visitor_can_browse_and_is_given_an_identity(tmp_path):
    app, _, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with visitor(app) as client:
            assert (await client.get("/api/agents")).status_code == 200
            assert GUEST_COOKIE in client.cookies, "a trial needs an identity of its own"
            assert valid_guest_id(client.cookies[GUEST_COOKIE])
            me = (await client.get("/api/auth/me")).json()
            assert me["data"] is None
            assert me["guest"]["turnsLeft"] == 4
            assert me["guest"]["turnsTotal"] == 4


async def test_installation_endpoints_still_need_an_account(tmp_path):
    app, _, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with visitor(app) as client:
            for method, path in [("GET", "/api/skills"), ("GET", "/api/mcp/servers"),
                                 ("POST", "/api/agents"), ("POST", "/api/sandbox/exec"),
                                 ("GET", "/api/sessions"), ("GET", "/api/traces/x")]:
                response = await client.request(method, path, json={})
                assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


async def test_the_fifth_turn_asks_for_an_account(tmp_path):
    app, _, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with visitor(app) as client:
            for index in range(4):
                reply = await client.post("/api/chat/completions", json=chat_body(f"q{index}"))
                assert reply.status_code == 200, reply.text
                left = (await client.get("/api/auth/me")).json()["guest"]["turnsLeft"]
                assert left == 3 - index
            blocked = await client.post("/api/chat/completions", json=chat_body("one more"))
            assert blocked.status_code == 403
            assert blocked.json()["detail"] == {"code": "guest_limit", "reason": "visitor"}
            # The refused attempt is not charged, so signing in is the only way
            # forward but the count has not drifted below zero.
            assert (await client.get("/api/auth/me")).json()["guest"]["turnsLeft"] == 0


async def test_the_daily_ceiling_covers_every_visitor(tmp_path):
    """Clearing a cookie makes a new visitor, so the per-visitor limit is not a budget."""
    app, _, _ = build(tmp_path, guest_daily_turns=2)
    async with app.router.lifespan_context(app):
        for _ in range(2):
            async with visitor(app) as client:
                assert (await client.post("/api/chat/completions",
                                          json=chat_body())).status_code == 200
        async with visitor(app) as third:
            blocked = await third.post("/api/chat/completions", json=chat_body())
            assert blocked.status_code == 403
            assert blocked.json()["detail"]["reason"] == "daily"
            assert (await third.get("/api/auth/me")).json()["guest"]["turnsLeft"] == 0


async def test_switching_the_trial_off_puts_the_wall_back(tmp_path):
    app, _, _ = build(tmp_path, guest_turns=0)
    async with app.router.lifespan_context(app):
        async with visitor(app) as client:
            assert (await client.get("/api/agents")).status_code == 401
            assert (await client.post("/api/chat/completions",
                                      json=chat_body())).status_code == 401
            # /me stays readable: a login page has to ask whether a session exists.
            assert (await client.get("/api/auth/me")).json()["guest"]["allowed"] is False


async def test_signing_in_keeps_the_trial_transcript(tmp_path):
    app, store, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with visitor(app) as client:
            await client.get("/api/auth/me")
            guest_id = client.cookies[GUEST_COOKIE]
            # What a trial produces: a session and its artifact, both owned by the guest.
            await store.put("sessions", {"id": "trial-session", "agent_id": "default"},
                            owner=guest_id)
            await store.put("artifacts", {"id": "trial-file", "sessionId": "trial-session"},
                            owner=guest_id)
            login = await client.post("/api/auth/login",
                                      json={"username": "admin", "password": TEST_PASSWORD})
            assert login.status_code == 200
            user_id = login.json()["data"]["id"]
            assert GUEST_COOKIE not in client.cookies, "the trial id is spent at sign-in"

        for collection, identity in [("sessions", "trial-session"),
                                     ("artifacts", "trial-file")]:
            row = await store.db[collection].find_one({"id": identity})
            assert row["owner_id"] == user_id, f"{collection} stayed with the visitor"
        assert await store.db.guest_usage.count_documents({"id": guest_id}) == 0


async def test_two_visitors_cannot_see_each_other(tmp_path):
    app, store, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        async with visitor(app) as first, visitor(app) as second:
            mine = first.cookies[GUEST_COOKIE] if GUEST_COOKIE in first.cookies else None
            if mine is None:
                await first.get("/api/auth/me")
                mine = first.cookies[GUEST_COOKIE]
            await second.get("/api/auth/me")
            assert mine != second.cookies[GUEST_COOKIE]
            await store.put("sessions", {"id": "mine", "agent_id": "default"}, owner=mine)
            assert (await second.get("/api/sessions/mine/context")).status_code == 404


async def test_abandoned_trials_are_pruned(tmp_path):
    app, store, _ = build(tmp_path)
    async with app.router.lifespan_context(app):
        stale = new_guest_id()
        fresh = new_guest_id()
        await store.db.guest_usage.insert_many([
            {"id": stale, "day": "2020-01-01", "turns": 2},
            {"id": fresh, "day": "2999-01-01", "turns": 1}])
        for identity, owner in [("old-session", stale), ("new-session", fresh)]:
            await store.db.sessions.insert_one({"id": identity, "owner_id": owner})
        await app.state.auth.prune_guests(72)
        assert await store.db.sessions.count_documents({"owner_id": stale}) == 0
        assert await store.db.sessions.count_documents({"owner_id": fresh}) == 1


def test_guest_ids_are_shape_checked():
    """A cookie is untrusted input even when forging one buys nothing."""
    identity = new_guest_id()
    assert valid_guest_id(identity) and is_guest(identity)
    for bad in ("", None, "guest_", "guest_short", "../../etc/passwd", "admin",
                "guest_" + "x" * 80):
        assert not valid_guest_id(bad)
    assert not is_guest("")
