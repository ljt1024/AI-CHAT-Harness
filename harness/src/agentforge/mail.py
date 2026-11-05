"""Outbound email for sign-in verification codes.

Two implementations: a console channel so the flow can be built and tested
without a mailbox, and SMTP because that is the one protocol every mail
provider speaks. `smtplib` is in the standard library, so a deployment needs no
new dependency -- the same rule the SMS senders follow.

The message carries a six-digit code and nothing else about the person. Codes
are never stored here: whether one was correct is decided in `auth.py`, against
a hashed, single-use record.
"""

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from agentforge import email_address
from agentforge.delivery import DeliveryError

logger = logging.getLogger(__name__)


class MailError(DeliveryError):
    """A send the operator has to see: bad credentials, refused relay."""


@dataclass(frozen=True)
class MailMessage:
    to: str        # normalised, e.g. alice@example.com
    code: str
    minutes: int


class ConsoleMailer:
    """Development channel: the code goes to the log and nowhere else.

    Deliberately never silent: an operator who forgot to configure SMTP must be
    able to notice, because a production deployment printing codes into a log
    file is a leak rather than a feature.
    """

    name = "console"

    def __init__(self, echo: bool = False):
        self.echo = echo

    async def deliver(self, to: str, code: str, minutes: int) -> None:
        logger.warning("Mail console channel: %s -> code %s (%d min). No message was sent.",
                       email_address.mask(to), code, minutes)


class SmtpMailer:
    """SMTP with either STARTTLS (587), implicit TLS (465) or plain (local dev)."""

    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str, sender: str,
                 tls: str = "starttls", timeout: float = 10, name: str = ""):
        missing = [label for label, value in (
            ("SMTP_HOST", host), ("SMTP_FROM", sender or user)) if not value]
        if missing:
            raise MailError(f"SMTP is missing {', '.join(missing)}.")
        if tls not in {"starttls", "ssl", "none"}:
            raise MailError(f"Unknown SMTP_TLS {tls!r}. Use starttls, ssl or none.")
        self.host, self.port = host, int(port)
        self.credentials = (user, password)
        # SMTP_FROM may already be written as "Name <a@b>"; the address is what
        # the envelope and the Message-ID need, so split it off instead of
        # carrying the angle brackets into a domain.
        embedded, address = parseaddr(sender or user)
        self.sender = address or (sender or user)
        self.name = name or embedded
        self.tls, self.timeout = tls, timeout

    # ------------------------------------------------------------------ message
    def message(self, to: str, code: str, minutes: int) -> EmailMessage:
        """A plain-text letter, kept separate so it can be asserted without a server."""
        message = EmailMessage()
        message["From"] = formataddr((self.name, self.sender)) if self.name else self.sender
        message["To"] = to
        message["Subject"] = f"登录验证码 {code}"
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or None)
        message.set_content(
            f"你的登录验证码是 {code}，{minutes} 分钟内有效。\n"
            "如果不是你本人操作，请忽略这封邮件。\n\n"
            f"Your verification code is {code}. It expires in {minutes} minutes.\n"
            "If you did not ask for it, please ignore this message.\n")
        return message

    # ---------------------------------------------------------------- transport
    def _send(self, message: EmailMessage) -> None:
        """Blocking on purpose: `deliver` runs it in a worker thread."""
        server = None
        try:
            if self.tls == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.tls == "starttls":
                    server.starttls()
            user, password = self.credentials
            if user:
                server.login(user, password)
            server.send_message(message)
        finally:
            if server is not None:
                try:
                    server.quit()
                except (smtplib.SMTPException, OSError):
                    server.close()

    async def deliver(self, to: str, code: str, minutes: int) -> None:
        message = self.message(to, code, minutes)
        try:
            await asyncio.to_thread(self._send, message)
        except (smtplib.SMTPException, OSError) as exc:
            # Never log the credentials or the code; the exception type is what
            # tells an operator whether it is DNS, auth or a refused relay.
            raise MailError(f"SMTP send failed: {type(exc).__name__}") from None


def build_mailer(settings) -> object | None:
    """Pick the configured channel, or None when it cannot work.

    A misconfigured provider returns None rather than falling back to the
    console channel: printing codes into the log while the operator believes
    mail is being delivered is worse than an honest "not configured".
    """
    provider = (settings.mail_provider or "console").strip().lower()
    if provider == "console":
        if settings.mail_debug_echo:
            logger.warning("Mail is on the console channel with echo enabled: codes are "
                           "returned by the API. Never use this on a public deployment.")
        else:
            logger.warning("Mail is on the console channel: codes are printed to this log "
                           "and never sent. Set MAIL_PROVIDER=smtp to deliver them.")
        return ConsoleMailer(echo=settings.mail_debug_echo)
    if provider == "smtp":
        try:
            return SmtpMailer(settings.smtp_host, settings.smtp_port,
                              settings.smtp_user, settings.smtp_password.get_secret_value(),
                              settings.smtp_from, settings.smtp_tls, settings.smtp_timeout,
                              settings.smtp_from_name)
        except MailError as exc:
            logger.error("%s Sign-in by email will answer 503 until it is fixed.", exc)
            return None
    logger.error("Unknown MAIL_PROVIDER %r. Sign-in by email is disabled.", provider)
    return None
