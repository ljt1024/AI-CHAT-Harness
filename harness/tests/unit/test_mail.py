"""The SMTP sender: what it puts on the wire, and what it refuses to pretend.

No network here: the message is built and inspected directly, and the one
transport test replaces `smtplib.SMTP` with a refusal so the wrapping into
`MailError` is exercised without a mailbox.
"""

import smtplib

import pytest

from agentforge.config import Settings
from agentforge.mail import ConsoleMailer, MailError, SmtpMailer, build_mailer


def mailer(**overrides):
    values = {"host": "smtp.example.com", "port": 587, "user": "bot@example.com",
              "password": "a-secret", "sender": "AI-CHAT <bot@example.com>",
              "tls": "starttls"}
    return SmtpMailer(**{**values, **overrides})


def test_the_letter_carries_the_code_and_the_expiry():
    message = mailer().message("alice@example.com", "123456", 5)
    assert message["To"] == "alice@example.com"
    assert message["From"] == "AI-CHAT <bot@example.com>"
    assert "123456" in message["Subject"]
    body = message.get_content()
    assert "123456" in body and "5 分钟" in body


def test_a_display_name_never_leaks_into_the_message_id():
    """The inbox label is free; the angle brackets are not."""
    message = mailer(name="AgentForge").message("alice@example.com", "123456", 5)
    assert message["From"] == "AgentForge <bot@example.com>"
    assert message["Message-ID"].endswith("@example.com>")


def test_a_display_name_baked_into_the_from_address_is_split_off():
    message = mailer(sender="AI-CHAT <bot@example.com>").message("alice@example.com", "123456", 5)
    assert message["From"] == "AI-CHAT <bot@example.com>"
    assert message["Message-ID"].endswith("@example.com>"), message["Message-ID"]


def test_a_missing_host_is_an_error_not_a_console_fallback():
    with pytest.raises(MailError):
        mailer(host="", sender="")


def test_an_unknown_tls_mode_is_refused():
    with pytest.raises(MailError):
        mailer(tls="maybe")


def test_build_mailer_returns_none_for_a_named_but_unconfigured_provider():
    settings = Settings(_env_file=None, mail_provider="smtp")
    assert build_mailer(settings) is None, "an honest 503 beats logging codes quietly"


def test_build_mailer_prefers_console_by_default():
    settings = Settings(_env_file=None, mail_provider="console")
    assert isinstance(build_mailer(settings), ConsoleMailer)


async def test_a_refused_relay_becomes_a_mail_error(monkeypatch):
    def refuse(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    with pytest.raises(MailError) as refused:
        await mailer().deliver("alice@example.com", "123456", 5)
    assert "SMTPAuthenticationError" in str(refused.value)
