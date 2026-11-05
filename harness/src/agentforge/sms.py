"""Outbound SMS for sign-up verification codes.

One interface, three implementations: the two clouds a Chinese deployment
actually uses, and a console channel that exists so the flow can be built and
tested before anyone buys a template.

Both clouds are reached over plain HTTPS with httpx, which the harness already
depends on. Their SDKs are large trees of code for a single API call, and the
deployment server builds its virtualenv without a package proxy -- same reason
`auth.py` uses `hashlib.scrypt` instead of argon2. The signature functions are
pure so they can be tested without a network or a credential.

Codes are never stored here: this module only carries them out. Whether a code
was correct is decided in `auth.py`, against a hashed, single-use record.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from agentforge import phone as phone_number
from agentforge.delivery import DeliveryError

logger = logging.getLogger(__name__)


class SmsError(DeliveryError):
    """A send the operator has to see: bad credentials, rejected template."""


@dataclass(frozen=True)
class SmsMessage:
    """The one message this platform sends, so the template has two parameters."""

    phone: str      # E.164, e.g. +8613800138000
    code: str
    minutes: int


class _SmsSender:
    """Adapter so `auth.py` can deliver without knowing which channel it holds.

    Mail has the same method, which is the whole point: the code lifecycle does
    not branch on how the message travels.
    """

    async def deliver(self, to: str, code: str, minutes: int) -> None:
        await self.send(SmsMessage(phone=to, code=code, minutes=minutes))


class ConsoleSender(_SmsSender):
    """Development channel: the code goes to the log and nowhere else.

    Deliberately never silent: an operator who forgot to configure a provider
    must be able to notice, because a production deployment printing codes into
    a log file is a leak rather than a feature.
    """

    name = "console"

    def __init__(self, echo: bool = False):
        self.echo = echo

    async def send(self, message: SmsMessage) -> None:
        logger.warning("SMS console channel: %s -> code %s (%d min). No message was sent.",
                       phone_number.mask(message.phone), message.code, message.minutes)


class TencentSender(_SmsSender):
    """Tencent Cloud SMS (SendSms, 2021-01-11) signed with TC3-HMAC-SHA256."""

    name = "tencent"
    host = "sms.tencentcloudapi.com"
    service = "sms"
    version = "2021-01-11"

    def __init__(self, secret_id: str, secret_key: str, app_id: str, sign_name: str,
                 template_id: str, region: str = "ap-guangzhou", timeout: float = 10):
        missing = [label for label, value in (
            ("SMS_SECRET_ID", secret_id), ("SMS_SECRET_KEY", secret_key),
            ("SMS_APP_ID", app_id), ("SMS_SIGN_NAME", sign_name),
            ("SMS_TEMPLATE", template_id)) if not value]
        if missing:
            raise SmsError(f"Tencent SMS is missing {', '.join(missing)}.")
        self.credentials = (secret_id, secret_key)
        self.app_id, self.sign_name, self.template_id = app_id, sign_name, template_id
        self.region, self.timeout = region, timeout

    def body(self, message: SmsMessage) -> bytes:
        return json.dumps({
            "PhoneNumberSet": [message.phone],
            "SmsSdkAppId": self.app_id,
            "SignName": self.sign_name,
            "TemplateId": self.template_id,
            # Parameter order is the template's, not ours: {1} is the code and
            # {2} the validity in minutes, which is how the template is written.
            "TemplateParamSet": [message.code, str(message.minutes)],
        }, separators=(",", ":"), ensure_ascii=False).encode()

    async def send(self, message: SmsMessage) -> None:
        payload = self.body(message)
        headers = tc3_headers(secret_id=self.credentials[0], secret_key=self.credentials[1],
                              service=self.service, host=self.host, action="SendSms",
                              version=self.version, payload=payload,
                              timestamp=int(time.time()), region=self.region)
        await _post(f"https://{self.host}", headers, payload, "Tencent", self.timeout)


class AliyunSender(_SmsSender):
    """Aliyun Dysmsapi (SendSms, 2017-05-25) signed with the RPC HMAC-SHA1 scheme."""

    name = "aliyun"
    endpoint = "https://dysmsapi.aliyuncs.com/"

    def __init__(self, key_id: str, key_secret: str, sign_name: str, template_code: str,
                 region: str = "cn-hangzhou", timeout: float = 10):
        missing = [label for label, value in (
            ("SMS_SECRET_ID", key_id), ("SMS_SECRET_KEY", key_secret),
            ("SMS_SIGN_NAME", sign_name), ("SMS_TEMPLATE", template_code)) if not value]
        if missing:
            raise SmsError(f"Aliyun SMS is missing {', '.join(missing)}.")
        self.credentials = (key_id, key_secret)
        self.sign_name, self.template_code = sign_name, template_code
        self.region, self.timeout = region, timeout

    def params(self, message: SmsMessage, nonce: str, timestamp: str) -> dict[str, str]:
        return {
            "AccessKeyId": self.credentials[0],
            "Action": "SendSms",
            "Format": "JSON",
            "PhoneNumbers": phone_number.local(message.phone),
            "RegionId": self.region,
            "SignName": self.sign_name,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": nonce,
            "SignatureVersion": "1.0",
            "TemplateCode": self.template_code,
            "TemplateParam": json.dumps({"code": message.code}, separators=(",", ":")),
            "Timestamp": timestamp,
            "Version": "2017-05-25",
        }

    async def send(self, message: SmsMessage) -> None:
        params = self.params(message, uuid.uuid4().hex,
                             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        params["Signature"] = aliyun_signature("GET", params, self.credentials[1])
        query = "&".join(f"{_encode(key)}={_encode(value)}"
                         for key, value in sorted(params.items()))
        await _get(f"{self.endpoint}?{query}", "Aliyun", self.timeout)


# ------------------------------------------------------------------ signatures
def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def tc3_headers(*, secret_id: str, secret_key: str, service: str, host: str, action: str,
                version: str, payload: bytes, timestamp: int, region: str = "",
                content_type: str = "application/json; charset=utf-8") -> dict[str, str]:
    """The headers Tencent Cloud requires.

    Kept separate from the request so the signature can be checked without a
    network: the parts that go wrong (header order, hashing the wrong string)
    are all in here.
    """
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    signed_headers = "content-type;host"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers,
        hashlib.sha256(payload).hexdigest()])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(timestamp), scope,
        hashlib.sha256(canonical_request.encode()).hexdigest()])
    signing_key = _hmac(_hmac(_hmac(f"TC3{secret_key}".encode(), date), service), "tc3_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


def _encode(value: str) -> str:
    """Aliyun's RFC 3986 rule: everything but unreserved characters is escaped."""
    return quote(str(value), safe="~")


def aliyun_signature(method: str, params: dict[str, str], secret: str) -> str:
    canonical = "&".join(f"{_encode(key)}={_encode(value)}"
                         for key, value in sorted(params.items()) if key != "Signature")
    string_to_sign = f"{method.upper()}&{_encode('/')}&{_encode(canonical)}"
    digest = hmac.new(f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


# -------------------------------------------------------------------- transport
async def _post(url: str, headers: dict[str, str], payload: bytes, label: str, timeout: float):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, content=payload)
    except httpx.HTTPError as exc:
        raise SmsError(f"{label} SMS request failed: {type(exc).__name__}") from None
    _check(response, label)


async def _get(url: str, label: str, timeout: float):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise SmsError(f"{label} SMS request failed: {type(exc).__name__}") from None
    _check(response, label)


def _check(response: httpx.Response, label: str):
    """Both clouds answer 200 with an error code inside the body."""
    if response.status_code != 200:
        raise SmsError(f"{label} SMS returned HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        raise SmsError(f"{label} SMS returned a non-JSON answer.") from None
    if label == "Tencent":
        answer = (body.get("Response") or {})
        error = answer.get("Error")
        if error:
            # Never log the credential, but the code is what an operator needs
            # to look up ("FailedOperation.TemplateIncorrectOrUnapproved").
            raise SmsError(f"Tencent SMS rejected the send: {error.get('Code')}")
        results = answer.get("SendStatusSet") or []
        if not results or results[0].get("Code") != "Ok":
            raise SmsError(f"Tencent SMS did not accept the number: "
                           f"{(results or [{}])[0].get('Code', 'no result')}")
        return
    if body.get("Code") != "OK":
        raise SmsError(f"Aliyun SMS rejected the send: {body.get('Code')}")


def build_sender(settings) -> object | None:
    """Pick the configured channel, or None when it cannot work.

    A misconfigured provider returns None rather than falling back to the
    console channel: printing codes into the log while the operator believes
    messages are being delivered is worse than an honest "not configured".
    """
    provider = (settings.sms_provider or "console").strip().lower()
    if provider == "console":
        if not settings.sms_debug_echo:
            logger.warning("SMS is on the console channel: codes are printed to this log "
                           "and never sent. Set SMS_PROVIDER to tencent or aliyun.")
        else:
            logger.warning("SMS is on the console channel with echo enabled: codes are "
                           "returned by the API. Never use this on a public deployment.")
        return ConsoleSender(echo=settings.sms_debug_echo)
    try:
        if provider == "tencent":
            return TencentSender(settings.sms_secret_id.get_secret_value(),
                                 settings.sms_secret_key.get_secret_value(),
                                 settings.sms_app_id, settings.sms_sign_name,
                                 settings.sms_template,
                                 settings.sms_region or "ap-guangzhou")
        if provider == "aliyun":
            return AliyunSender(settings.sms_secret_id.get_secret_value(),
                                settings.sms_secret_key.get_secret_value(),
                                settings.sms_sign_name, settings.sms_template,
                                settings.sms_region or "cn-hangzhou")
    except SmsError as exc:
        logger.error("%s Sign-in by phone will answer 503 until it is fixed.", exc)
        return None
    logger.error("Unknown SMS_PROVIDER %r. Sign-in by phone is disabled.", provider)
    return None
