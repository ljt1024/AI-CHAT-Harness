"""Sign-up by phone number, over the same HTTP surface the browser uses.

What is being protected here: one number is one account, the code is the only
way in, the number never leaves the server unmasked, and a visitor who was
chatting anonymously keeps that transcript after signing up.
"""

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.registry import ModelGateway
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD

PHONE = "13800138000"


async def make_app(tmp_path, **overrides):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path,
                        sandbox_warm_size=0, **{"sms_debug_echo": True,
                                                "verify_cooldown": 0,
                                                "verify_daily_per_address": 50,
                                                "verify_daily_per_ip": 200, **overrides})
    app = create_app(settings, store, ModelGateway())
    return app, store


@pytest.fixture
async def service(tmp_path):
    app, store = await make_app(tmp_path)
    async with app.router.lifespan_context(app):
        yield app, store


def anonymous(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def request_code(client, phone=PHONE, purpose="login"):
    return await client.post("/api/auth/sms/code", json={"phone": phone, "purpose": purpose})


async def sign_up(client, phone=PHONE):
    """Ask for a code, read the development echo, and exchange it for a session."""
    sent = await request_code(client, phone)
    assert sent.status_code == 200, sent.text
    code = sent.json()["data"]["debugCode"]
    return await client.post("/api/auth/sms/verify", json={"phone": phone, "code": code})


async def test_an_unknown_number_becomes_an_account(service):
    app, store = service
    async with anonymous(app) as client:
        answer = await sign_up(client)
        assert answer.status_code == 200
        assert answer.json()["data"]["phone"] == "138****8000", "the full number stays server-side"
        assert answer.json()["data"]["displayName"] == "138****8000"

        me = (await client.get("/api/auth/me")).json()["data"]
        assert me["phone"] == "138****8000"
        assert await store.db.users.count_documents({"phone": "+8613800138000"}) == 1

        # Signed in now: the endpoints that used to answer 401 do not.
        assert (await client.get("/api/agents")).status_code == 200


async def test_the_same_number_signs_in_rather_than_registering_again(service):
    app, store = service
    async with anonymous(app) as client:
        first = (await sign_up(client)).json()["data"]
        await client.post("/api/auth/logout")
        second = (await sign_up(client)).json()["data"]
    assert first["id"] == second["id"]
    # The account `bootstrap()` made is still there; only one number is on file.
    assert await store.db.users.count_documents({"phone": {"$exists": True}}) == 1


async def test_a_wrong_code_is_refused_and_says_why(service):
    app, _ = service
    async with anonymous(app) as client:
        await request_code(client)
        answer = await client.post("/api/auth/sms/verify",
                                   json={"phone": PHONE, "code": "000000"})
        assert answer.status_code == 400
        assert answer.json()["detail"]["code"] == "invalid"
        assert (await client.get("/api/auth/me")).json()["data"] is None


async def test_a_malformed_number_is_a_422_not_a_500(service):
    app, _ = service
    async with anonymous(app) as client:
        answer = await request_code(client, "12345")
    assert answer.status_code == 422


async def test_binding_adds_a_number_to_an_existing_account(service):
    app, store = service
    async with anonymous(app) as client:
        # The account `bootstrap()` made has a password and no phone.
        assert (await client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})).status_code == 200
        assert (await sign_up(client, "13900139000")).status_code == 200
        await client.post("/api/auth/logout")

        assert (await client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})).status_code == 200
        sent = await request_code(client, PHONE, purpose="bind")
        assert sent.status_code == 200, sent.text
        bound = await client.post("/api/auth/phone", json={
            "phone": PHONE, "code": sent.json()["data"]["debugCode"]})
        assert bound.status_code == 200
        assert bound.json()["data"]["phone"] == "138****8000"
        assert (await client.get("/api/auth/me")).json()["data"]["displayName"] == "admin"

        # An unbound caller cannot use the bind purpose at all.
        await client.post("/api/auth/logout")
        assert (await request_code(client, "13700137000", purpose="bind")).status_code == 401
    assert await store.db.users.count_documents({"phone": "+8613800138000"}) == 1


async def test_a_number_cannot_be_bound_twice(service):
    app, _ = service
    async with anonymous(app) as client:
        await sign_up(client, PHONE)
        await client.post("/api/auth/logout")
        assert (await client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})).status_code == 200
    async with anonymous(app) as admin:
        assert (await admin.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})).status_code == 200
        answer = await request_code(admin, PHONE, purpose="bind")
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "phone_taken"


async def test_closing_signup_stops_new_numbers_at_the_door(tmp_path):
    app, store = await make_app(tmp_path, signup_open=False)
    async with app.router.lifespan_context(app):
        async with anonymous(app) as client:
            answer = await request_code(client)
            assert answer.status_code == 403
            assert answer.json()["detail"]["code"] == "signup_closed"
    assert await store.db.sms_codes.count_documents({}) == 0, "no message was paid for"


async def test_the_cooldown_answers_with_a_countdown(tmp_path):
    app, _ = await make_app(tmp_path, verify_cooldown=60)
    async with app.router.lifespan_context(app):
        async with anonymous(app) as client:
            assert (await request_code(client)).status_code == 200
            again = await request_code(client)
    assert again.status_code == 429
    assert again.json()["detail"]["code"] == "cooldown"
    assert again.json()["detail"]["retryAfter"] > 0


async def test_an_anonymous_transcript_follows_the_new_account(service):
    app, store = service
    async with anonymous(app) as client:
        # Asking who we are is enough to be issued a visitor identity.
        await client.get("/api/auth/me")
        guest = next(cookie.value for cookie in client.cookies.jar
                     if cookie.name == "agentforge_guest")
        await store.db.sessions.insert_one({"id": "trial", "owner_id": guest, "title": "trial"})

        user = (await sign_up(client)).json()["data"]
        moved = await store.db.sessions.find_one({"id": "trial"})
    assert moved["owner_id"] == user["id"]
    assert await store.db.sessions.count_documents({"owner_id": guest}) == 0
