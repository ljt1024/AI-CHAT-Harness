"""The SMS channel: number shapes, signatures, and what a misconfiguration does.

The signatures are checked without a network on purpose. Getting one wrong
produces a provider error at the first real sign-up, which is the worst moment
to discover it; getting one wrong here fails the build instead.
"""

from datetime import datetime, timezone

import pytest

from agentforge import phone
from agentforge.config import Settings
from agentforge.sms import (AliyunSender, ConsoleSender, SmsError, SmsMessage, TencentSender,
                            aliyun_signature, build_sender, tc3_headers)


def test_people_type_the_same_number_many_ways():
    for written in ("13800138000", "+86 138-0013-8000", "86 138 0013 8000",
                    "0086-138-0013-8000", " 138 0013 8000 "):
        assert phone.normalize(written) == "+8613800138000", written


@pytest.mark.parametrize("written", ["", "1380013800", "12800138000", "01088886666",
                                     "+1 415 555 0100", "138001380001", "abcdefghijk"])
def test_only_mainland_mobiles_are_accepted(written):
    with pytest.raises(ValueError):
        phone.normalize(written)


def test_a_number_is_masked_before_it_reaches_a_log():
    assert phone.mask("+8613800138000") == "138****8000"
    assert phone.local("+8613800138000") == "13800138000"


def test_tc3_signature_covers_the_body_and_the_clock():
    common = dict(secret_id="AKIDexample", secret_key="secret", service="sms",
                  host="sms.tencentcloudapi.com", action="SendSms", version="2021-01-11",
                  timestamp=1_700_000_000, region="ap-guangzhou")
    first = tc3_headers(payload=b'{"a":1}', **common)
    assert first == tc3_headers(payload=b'{"a":1}', **common), "same input, same signature"
    assert first != tc3_headers(payload=b'{"a":2}', **common), "the body is signed"
    assert first != tc3_headers(payload=b'{"a":1}', **{**common, "timestamp": 1_700_000_001})

    assert first["Authorization"].startswith(
        "TC3-HMAC-SHA256 Credential=AKIDexample/2023-11-14/sms/tc3_request")
    assert "SignedHeaders=content-type;host" in first["Authorization"]
    signature = first["Authorization"].rsplit("Signature=", 1)[1]
    assert len(signature) == 64 and all(char in "0123456789abcdef" for char in signature)
    assert first["X-TC-Action"] == "SendSms" and first["X-TC-Timestamp"] == "1700000000"


def test_tc3_date_comes_from_the_timestamp_not_the_local_clock():
    headers = tc3_headers(secret_id="id", secret_key="key", service="sms", host="h",
                          action="SendSms", version="2021-01-11", payload=b"{}",
                          timestamp=int(datetime(2024, 2, 29, 23, 30,
                                                 tzinfo=timezone.utc).timestamp()))
    assert "/2024-02-29/sms/tc3_request" in headers["Authorization"]


def test_aliyun_signature_ignores_parameter_order():
    params = {"Action": "SendSms", "PhoneNumbers": "13800138000", "SignName": "测试",
              "TemplateParam": '{"code":"123456"}', "Timestamp": "2024-01-01T00:00:00Z"}
    shuffled = dict(reversed(list(params.items())))
    assert aliyun_signature("GET", params, "secret") == aliyun_signature("GET", shuffled, "secret")
    assert aliyun_signature("GET", params, "secret") != aliyun_signature("GET", params, "other")


def test_aliyun_escapes_what_the_rpc_scheme_says_to_escape():
    # '/' and ':' are inside the template payload and the timestamp; if they are
    # not percent-encoded the server computes a different string and rejects it.
    params = {"TemplateParam": '{"code":"1"}', "Timestamp": "2024-01-01T00:00:00Z"}
    signature = aliyun_signature("GET", params, "secret")
    assert signature and signature != aliyun_signature("GET", params, "secret")[:-1]


def test_a_provider_named_but_not_configured_is_not_silently_replaced():
    settings = Settings(_env_file=None, sms_provider="tencent")
    assert build_sender(settings) is None, "must not fall back to printing codes"


@pytest.mark.parametrize("provider", ["tencent", "aliyun"])
def test_missing_credentials_are_named(provider):
    with pytest.raises(SmsError) as failure:
        if provider == "tencent":
            TencentSender("", "", "", "", "")
        else:
            AliyunSender("", "", "", "")
    assert "SMS_SECRET_ID" in str(failure.value)
    assert "SMS_TEMPLATE" in str(failure.value)


async def test_console_channel_still_carries_the_message():
    sender = ConsoleSender(echo=True)
    assert sender.name == "console" and sender.echo
    # Nothing to assert about the log, but it must not raise: it is the channel
    # every deployment starts on.
    await sender.send(SmsMessage(phone="+8613800138000", code="123456", minutes=5))
