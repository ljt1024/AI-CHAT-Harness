"""Email verification codes: identity shape, one account per address, burn-once.

The lifecycle is shared with SMS; what is specific here is the address
normalisation, the derived username, and the fact that a consumed code keeps
holding the cooldown so a sign-in cannot immediately ask for another message.
"""

from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge import email_address
from agentforge.auth import AuthService, SmsCodeError
from agentforge.delivery import DeliveryError
from agentforge.store import MongoDBStore


class FakeMailer:
    name = "fake"
    echo = True

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str, int]] = []
        self.fail = fail

    async def deliver(self, to: str, code: str, minutes: int) -> None:
        if self.fail:
            raise DeliveryError("provider said no")
        self.sent.append((to, code, minutes))


class FakeSms:
    async def deliver(self, to: str, code: str, minutes: int) -> None:
        pass


def make_service(**kwargs):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    defaults = {"cooldown": 0}
    return AuthService(store, **{**defaults, **kwargs}), store


async def issue(service, address, mailer, ip="", purpose="login"):
    return await service.issue_code(address, purpose, ip, mailer, channel="email")


async def verify(service, address, code, purpose="login"):
    return await service.verify_code(address, purpose, code, channel="email")


def test_an_address_is_normalised_before_it_is_stored():
    assert email_address.normalize(" Alice@Example.COM ") == "alice@example.com"
    assert email_address.normalize("a.b+tag@sub.example.cn") == "a.b+tag@sub.example.cn"
    assert email_address.mask("alice@example.com") == "a***@example.com"
    assert email_address.mask("a@example.com") == "a***@example.com"
    for bad in ("alice", "alice@", "@example.com", "a..b@example.com", "alice@example",
                "alice@example.c", "alice@-example.com"):
        with pytest.raises(ValueError):
            email_address.normalize(bad)


async def test_a_code_goes_to_the_normalised_address_and_is_correct_once():
    service, _ = make_service()
    mailer = FakeMailer()
    sent = await issue(service, "Alice@Example.com", mailer)

    assert mailer.sent[0][0] == "alice@example.com"
    assert mailer.sent[0][1] == sent["code"] and len(sent["code"]) == 6
    assert await verify(service, " ALICE@example.com ", sent["code"]) == "alice@example.com"
    with pytest.raises(SmsCodeError) as second:
        await verify(service, "alice@example.com", sent["code"])
    assert second.value.reason == "expired"


async def test_a_consumed_code_still_holds_the_cooldown():
    service, _ = make_service(cooldown=60)
    mailer = FakeMailer()
    sent = await issue(service, "alice@example.com", mailer)
    await verify(service, "alice@example.com", sent["code"])

    # The row is marked consumed rather than deleted, so this is a refusal, not
    # a free second message the moment someone signs in.
    with pytest.raises(SmsCodeError) as too_soon:
        await issue(service, "alice@example.com", mailer)
    assert too_soon.value.reason == "cooldown"
    assert len(mailer.sent) == 1


async def test_a_channel_cannot_answer_for_the_other():
    service, store = make_service()
    mailer = FakeMailer()
    sent = await issue(service, "alice@example.com", mailer)
    rows = await store.db.verification_codes.find({}).to_list(length=10)
    assert rows[0]["channel"] == "email"
    # An SMS verification asks the phone-shaped query; the email row is invisible
    # to it, so there is nothing to match even with the right digits.
    with pytest.raises(ValueError):
        await service.verify_code("alice@example.com", "login", sent["code"], channel="sms")


async def test_a_failed_send_leaves_no_row_and_refunds_the_budget():
    service, store = make_service(per_address=1)
    with pytest.raises(SmsCodeError) as refused:
        await issue(service, "alice@example.com", FakeMailer(fail=True))
    assert refused.value.reason == "send_failed"
    assert await store.db.verification_codes.count_documents({}) == 0
    assert await store.db.mail_sends.count_documents({"count": {"$gt": 0}}) == 0
    await issue(service, "alice@example.com", FakeMailer())


async def test_a_new_address_becomes_exactly_one_account():
    service, _ = make_service()
    first = await service.create_email_user("alice@example.com")
    second = await service.create_email_user("alice@example.com")
    assert first["id"] == second["id"]
    assert first["email"] == "alice@example.com"
    assert first["display_name"] == "a***@example.com", "the mask is what the sidebar shows"
    assert first["username"].startswith("e")
    assert "password" not in first, "nothing to guess: the code is the only door"


async def test_binding_an_address_someone_else_owns_is_refused():
    service, _ = make_service()
    owner = await service.create_email_user("alice@example.com")
    other = await service.create_user("bob", "a-long-enough-password")
    with pytest.raises(SmsCodeError) as taken:
        await service.bind_email(other["id"], "alice@example.com")
    assert taken.value.reason == "email_taken"
    assert (await service.user_by_email("alice@example.com"))["id"] == owner["id"]

    bound = await service.bind_email(other["id"], "bob@example.com")
    assert bound["email"] == "bob@example.com"
    assert bound["display_name"] == "bob", "binding is not a rename"
