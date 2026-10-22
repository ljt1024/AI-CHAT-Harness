from copy import deepcopy

import pytest

from agentforge.prompts import RESPONSE_LANGUAGE_POLICY, with_response_language


@pytest.mark.parametrize("user_text", ["查看技能", "请用英文回答：技能是什么？", "What is a skill?"])
@pytest.mark.parametrize("system", [True, False])
async def test_language_policy_is_transient_and_keeps_explicit_override(user_text, system):
    messages = ([{"role": "system", "content": "Follow the installed skill instructions."}] if system else [])
    messages += [{"role": "user", "content": user_text},
                 {"role": "assistant", "content": "I'll read the English skill instructions."},
                 {"role": "user", "content": "Repeated tool calls made no progress. Reflect."}]
    original = deepcopy(messages)
    for _ in range(2):
        prepared = with_response_language(messages, user_text)
        assert RESPONSE_LANGUAGE_POLICY in prepared[0]["content"]
        assert RESPONSE_LANGUAGE_POLICY in prepared[-1]["content"]
        assert prepared[0]["content"].count(RESPONSE_LANGUAGE_POLICY) == 1
        assert "explicitly" in prepared[-1]["content"]
        assert ("Chinese (zh-CN)" in prepared[-1]["content"]) is (user_text != "What is a skill?")
        assert messages == original


async def test_language_policy_keeps_tool_manifest_and_original_request_identity():
    messages = [{"role": "system", "content": "Skill: English design instructions."},
                {"role": "user", "content": "使用设计技能"},
                {"role": "system", "content": "Current runtime capabilities: load_skill"}]
    prepared = with_response_language(messages, "使用设计技能")
    assert prepared[-1]["content"].startswith("Current runtime capabilities: load_skill")
    assert "FIRST sentence before calling tools" in prepared[0]["content"]
