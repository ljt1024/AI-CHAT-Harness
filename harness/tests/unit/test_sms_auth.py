"""Verification codes: what one costs, how long it lives, and when it dies.

The interesting cases are the refusals. A code that can be requested without
limit is a way to spend the operator's SMS budget; one that survives five wrong
guesses is a six-digit secret with a five-minute life and a script on the other
end; one that is not consumed lets a leaked message log in twice.
"""

from datetime import datetime, timedelta, timezone

from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.auth import AuthService, SmsCodeError
from agentforge.sms import SmsError, SmsMessage
from agentforge.store import MongoDBStore


class FakeSender:
    name = "fake"
    echo = True

    def __init__(self, fail: bool = False):
        self.sent: list[SmsMessage] = []
        self.fail = fail

    async def send(self, message: SmsMessage) -> None:
        if self.fail:
            raise SmsError("provider said no")
        self.sent.append(message)


def make_service(**kwargs):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    defaults = {"cooldown": 0}
    return AuthService(store, **{**defaults, **kwargs}), store


async def test_a_code_is_correct_once():
    service, _ = make_service()
    sender = FakeSender()
    sent = await service.issue_code("13800138000", "login", "1.2.3.4", sender)

    assert sender.sent[0].phone == "+8613800138000"
    assert sender.sent[0].code == sent["code"] and len(sent["code"]) == 6
    assert await service.verify_code("+86 138-0013-8000", "login", sent["code"]) \
        == "+8613800138000"
    with pytest.raises(SmsCodeError) as second:
        await service.verify_code("13800138000", "login", sent["code"])
    assert second.value.reason == "expired", "a used code leaves nothing to check against"


async def test_five_wrong_guesses_burn_the_code():
    service, store = make_service()
    sender = FakeSender()
    sent = await service.issue_code("13800138000", "login", "", sender)

    for _ in range(4):
        with pytest.raises(SmsCodeError) as wrong:
            await service.verify_code("13800138000", "login", "000000")
        assert wrong.value.reason == "invalid"
    with pytest.raises(SmsCodeError) as burnt:
        await service.verify_code("13800138000", "login", "000000")
    assert burnt.value.reason == "too_many"
    assert await store.db.sms_codes.count_documents({}) == 0
    # Even the right code is gone now: the point is to make guessing expensive.
    with pytest.raises(SmsCodeError):
        await service.verify_code("13800138000", "login", sent["code"])


async def test_a_code_stops_working_when_it_expires():
    service, store = make_service()
    sender = FakeSender()
    sent = await service.issue_code("13800138000", "login", "", sender)
    await store.db.sms_codes.update_one({}, {"$set": {
        "expire_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    with pytest.raises(SmsCodeError) as expired:
        await service.verify_code("13800138000", "login", sent["code"])
    assert expired.value.reason == "expired"


async def test_a_second_message_waits_for_the_cooldown():
    service, _ = make_service(cooldown=60)
    sender = FakeSender()
    await service.issue_code("13800138000", "login", "", sender)
    with pytest.raises(SmsCodeError) as too_soon:
        await service.issue_code("13800138000", "login", "", sender)
    assert too_soon.value.reason == "cooldown"
    assert 0 < too_soon.value.retry_after <= 60
    assert len(sender.sent) == 1


async def test_one_number_cannot_be_messaged_all_day():
    service, _ = make_service(per_phone=2, per_ip=99)
    sender = FakeSender()
    for _ in range(2):
        await service.issue_code("13800138000", "login", "", sender)
    with pytest.raises(SmsCodeError) as capped:
        await service.issue_code("13800138000", "login", "", sender)
    assert capped.value.reason == "daily_limit"
    assert capped.value.retry_after > 0


async def test_one_address_cannot_walk_through_every_number():
    service, _ = make_service(per_ip=2, per_phone=99)
    sender = FakeSender()
    for suffix in ("000", "001"):
        await service.issue_code(f"13800138{suffix}", "login", "9.9.9.9", sender)
    with pytest.raises(SmsCodeError) as capped:
        await service.issue_code("13800138002", "login", "9.9.9.9", sender)
    assert capped.value.reason == "daily_limit"


async def test_a_failed_send_leaves_no_trace_and_no_wait():
    service, store = make_service(cooldown=60, per_phone=1)
    with pytest.raises(SmsCodeError) as refused:
        await service.issue_code("13800138000", "login", "", FakeSender(fail=True))
    assert refused.value.reason == "send_failed"
    assert await store.db.sms_codes.count_documents({}) == 0
    assert await store.db.sms_sends.count_documents({"count": {"$gt": 0}}) == 0
    # The budget and the cooldown both handed back, so a provider outage does
    # not lock this person out for a minute or for the day.
    await service.issue_code("13800138000", "login", "", FakeSender())


async def test_a_phone_signup_creates_exactly_one_account():
    service, _ = make_service()
    first = await service.create_phone_user("+8613800138000")
    second = await service.create_phone_user("+8613800138000")
    assert first["id"] == second["id"]
    assert first["phone"] == "+8613800138000"
    assert first["display_name"] == "138****8000", "the mask is what the sidebar shows"
    assert "password" not in first, "nothing to guess: the code is the only door"


async def test_binding_a_number_someone_else_owns_is_refused():
    service, _ = make_service()
    owner = await service.create_phone_user("+8613800138000")
    other = await service.create_user("alice", "a-long-enough-password")
    with pytest.raises(SmsCodeError) as taken:
        await service.bind_phone(other["id"], "+8613800138000")
    assert taken.value.reason == "phone_taken"
    assert (await service.user_by_phone("+8613800138000"))["id"] == owner["id"]

    bound = await service.bind_phone(other["id"], "+8613900139000")
    assert bound["phone"] == "+8613900139000"
    assert bound["display_name"] == "alice", "binding is not a rename"
