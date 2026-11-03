"""Sign-up by email, over the same HTTP surface the browser uses.

What is being protected: one address is one account, the code is the only way
in, the address never leaves the server unmasked, closing registration does not
spend a message, and binding an address is only for a signed-in account.
"""

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.registry import ModelGateway
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD, TEST_USER

EMAIL = "Alice@Example.com"
NORMAL = "alice@example.com"


async def make_app(tmp_path, **overrides):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path, sandbox_warm_size=0,
                        **{"mail_debug_echo": True, "verify_cooldown": 0,
                           "verify_daily_per_address": 50, "verify_daily_per_ip": 200,
                           **overrides})
    app = create_app(settings, store, ModelGateway())
    return app, store


@pytest.fixture
async def service(tmp_path):
    app, store = await make_app(tmp_path)
    async with app.router.lifespan_context(app):
        yield app, store


def anonymous(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def request_code(client, email=EMAIL, purpose="login"):
    return await client.post("/api/auth/email/code", json={"email": email, "purpose": purpose})


async def sign_up(client, email=EMAIL):
    """Ask for a code, read the development echo, and exchange it for a session."""
    sent = await request_code(client, email)
    assert sent.status_code == 200, sent.text
    code = sent.json()["data"]["debugCode"]
    return await client.post("/api/auth/email/verify", json={"email": email, "code": code})


async def login_admin(client):
    answer = await client.post("/api/auth/login",
                               json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert answer.status_code == 200, answer.text


async def test_an_unknown_address_becomes_an_account(service):
    app, store = service
    async with anonymous(app) as client:
        answer = await sign_up(client)
        assert answer.status_code == 200
        data = answer.json()["data"]
        assert data["email"] == "a***@example.com", "the full address stays server-side"
        assert data["displayName"] == "a***@example.com"
        assert data["phone"] == ""

        me = (await client.get("/api/auth/me")).json()["data"]
        assert me["email"] == "a***@example.com"
        assert await store.db.users.count_documents({"email": NORMAL}) == 1

        # Signed in now: the endpoints that used to answer 401 do not.
        assert (await client.get("/api/agents")).status_code == 200


async def test_the_same_address_signs_in_rather_than_registering_again(service):
    app, store = service
    async with anonymous(app) as client:
        first = (await sign_up(client)).json()["data"]
        await client.post("/api/auth/logout")
        second = (await sign_up(client, NORMAL)).json()["data"]
    assert first["id"] == second["id"]
    assert await store.db.users.count_documents({"email": NORMAL}) == 1


async def test_a_wrong_code_is_refused_and_says_why(service):
    app, _ = service
    async with anonymous(app) as client:
        await request_code(client)
        answer = await client.post("/api/auth/email/verify",
                                   json={"email": EMAIL, "code": "000000"})
        assert answer.status_code == 400
        assert answer.json()["detail"]["code"] == "invalid"
        assert (await client.get("/api/auth/me")).json()["data"] is None


async def test_a_malformed_address_is_a_validation_error(service):
    app, store = service
    async with anonymous(app) as client:
        answer = await request_code(client, "not-an-address")
        assert answer.status_code == 422
        assert await store.db.verification_codes.count_documents({}) == 0


async def test_closing_signup_refuses_an_unknown_address_without_sending(tmp_path):
    app, store = await make_app(tmp_path, signup_open=False)
    async with app.router.lifespan_context(app):
        async with anonymous(app) as client:
            answer = await request_code(client, "nobody@example.com")
            assert answer.status_code == 403
            assert answer.json()["detail"]["code"] == "signup_closed"
            assert await store.db.mail_sends.count_documents({}) == 0


async def test_binding_needs_a_session(service):
    app, _ = service
    async with anonymous(app) as client:
        answer = await client.post("/api/auth/email",
                                   json={"email": EMAIL, "code": "123456"})
        # The middleware refuses it before the handler: it is not in PUBLIC_API,
        # which is exactly the default the auth model promises for a new route.
        assert answer.status_code == 401


async def test_a_signed_in_account_can_bind_an_address(service):
    app, store = service
    async with anonymous(app) as client:
        await login_admin(client)
        sent = await request_code(client, EMAIL, purpose="bind")
        assert sent.status_code == 200, sent.text
        code = sent.json()["data"]["debugCode"]
        answer = await client.post("/api/auth/email", json={"email": EMAIL, "code": code})
        assert answer.status_code == 200
        assert answer.json()["data"]["email"] == "a***@example.com"
        assert await store.db.users.count_documents({"email": NORMAL}) == 1


async def test_binding_an_address_another_account_owns_is_refused(service):
    app, _ = service
    async with anonymous(app) as client:
        await sign_up(client)          # this account owns alice@example.com
        await client.post("/api/auth/logout")
        await login_admin(client)      # the operator account
        answer = await request_code(client, NORMAL, purpose="bind")
        assert answer.status_code == 409
        assert answer.json()["detail"]["code"] == "email_taken"
