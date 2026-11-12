"""Password hashing, session lifecycle and login throttling."""

import asyncio

from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.auth import (AuthError, AuthService, hash_password, verify_password)
from agentforge.config import Settings
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD


def make_service(**kwargs):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    return AuthService(store, **kwargs), store


def test_hash_is_salted_and_verifiable():
    first, second = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert first != second, "each hash must carry its own salt"
    assert verify_password("correct horse battery", first)
    assert verify_password("correct horse battery", second)
    assert not verify_password("correct horse batterY", first)


def test_verification_rejects_malformed_records_without_raising():
    for record in ("", "plaintext", "scrypt$1$2$3", "bcrypt$1$2$3$c2FsdA==$aGFzaA==",
                   "scrypt$16384$8$1$not-base64$not-base64"):
        assert verify_password("anything", record) is False


async def test_password_rules_are_enforced():
    service, _ = make_service()
    with pytest.raises(ValueError, match="Password"):
        await service.create_user("alice", "short")
    with pytest.raises(ValueError, match="Username"):
        await service.create_user("1alice", "long-enough-password")
    created = await service.create_user("Alice", TEST_PASSWORD)
    assert created["username"] == "alice", "usernames are case-insensitive and stored folded"
    assert "password" not in created, "the hash never leaves the service"


async def test_login_creates_a_revocable_session():
    service, _ = make_service()
    user = await service.create_user("alice", TEST_PASSWORD)
    token = await service.start_session(user["id"])
    assert (await service.user_for_token(token))["id"] == user["id"]
    await service.end_session(token)
    assert await service.user_for_token(token) is None
    assert await service.user_for_token("") is None


async def test_sessions_expire():
    service, _ = make_service(session_hours=-0.0003)
    user = await service.create_user("alice", TEST_PASSWORD)
    token = await service.start_session(user["id"])
    await asyncio.sleep(0.05)
    assert await service.user_for_token(token) is None
    assert await service.prune() >= 1


async def test_revoking_a_user_ends_every_session():
    service, _ = make_service()
    user = await service.create_user("alice", TEST_PASSWORD)
    tokens = [await service.start_session(user["id"]) for _ in range(3)]
    assert await service.revoke_user(user["id"]) == 3
    for token in tokens:
        assert await service.user_for_token(token) is None


async def test_wrong_password_and_unknown_user_look_the_same():
    service, _ = make_service()
    await service.create_user("alice", TEST_PASSWORD)
    with pytest.raises(AuthError, match="Wrong username or password"):
        await service.authenticate("alice", "wrong-password-here")
    with pytest.raises(AuthError, match="Wrong username or password"):
        await service.authenticate("nobody", "wrong-password-here")


async def test_repeated_failures_lock_the_account_briefly():
    service, _ = make_service(max_failures=2, lockout_seconds=60)
    await service.create_user("alice", TEST_PASSWORD)
    for _ in range(2):
        with pytest.raises(AuthError):
            await service.authenticate("alice", "wrong-password-here")
    with pytest.raises(AuthError, match="Too many failed attempts"):
        await service.authenticate("alice", TEST_PASSWORD)
    service.clear_failures()
    assert (await service.authenticate("alice", TEST_PASSWORD))["username"] == "alice"


async def test_bootstrap_creates_one_account_and_stays_idempotent():
    service, _ = make_service()
    first = await service.bootstrap("admin", TEST_PASSWORD)
    second = await service.bootstrap("admin", "another-password-entirely")
    assert first["id"] == second["id"]
    assert await service.bootstrap("admin", "") is None, "no password means no account"
    assert (await service.authenticate("admin", TEST_PASSWORD))["id"] == first["id"]


async def test_bootstrap_refuses_a_short_password_instead_of_failing_open():
    service, _ = make_service()
    assert await service.bootstrap("admin", "short") is None
    with pytest.raises(AuthError):
        await service.authenticate("admin", "short")


async def test_legacy_resources_are_bound_once():
    service, store = make_service()
    user = await service.create_user("alice", TEST_PASSWORD)
    await store.db.skills.insert_one({"id": "demo-0123456789ab"})
    counts = await service.adopt_ownerless(user["id"])
    assert counts["skills"] == 1
    assert (await store.db.skills.find_one({"id": "demo-0123456789ab"}))["owner_id"] == user["id"]
    again = await service.adopt_ownerless(user["id"])
    assert sum(again.values()) == 0, "a second run has nothing left to adopt"


def test_session_settings_default_to_fail_closed():
    settings = Settings(_env_file=None)
    assert settings.admin_password.get_secret_value() == ""
    assert settings.cookie_secure is False
    assert settings.session_hours > 0
