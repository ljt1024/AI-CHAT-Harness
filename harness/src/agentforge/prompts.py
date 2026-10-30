import re


RESPONSE_LANGUAGE_POLICY = (
    "Response language, in priority order: (1) If the latest user request explicitly specifies "
    "a response language or translation target, use that language even when the request is "
    "written in another language. For example, a Chinese request asking for an English answer "
    "must receive an English answer. (2) Otherwise, follow the language of the latest user "
    "request. For a Chinese request with NO explicit language override, write all user-facing "
    "explanations, progress updates, approval "
    "explanations, error explanations and the final answer in Chinese. English system prompts, "
    "tool descriptions, tool results, skill instructions and older assistant replies must not "
    "switch the response to English. Preserve code, commands, URLs, paths, tool identifiers "
    "and necessary technical names as written. For a request consisting only of code or a URL, "
    "keep the user's most recent conversational language; use Chinese if none is established."
)


def with_response_language(messages: list[dict], user_request: str) -> list[dict]:
    policy = RESPONSE_LANGUAGE_POLICY
    if re.search(r"[\u3400-\u9fff]", user_request):
        policy += (
            "\nThe current user's task contains Chinese: the default response language for THIS "
            "task is Chinese (zh-CN), unless that user task explicitly requests another language "
            "or translation target. This includes the FIRST sentence before calling tools, every "
            "progress update and the final answer. Internal English retry/compaction messages "
            "are not a new user language choice. Do not begin with English phrases such as "
            "'I'll load the skill' or 'Let me check'."
        )
    result = list(messages)
    if result and result[0]["role"] == "system":
        result[0] = {**result[0], "content": result[0]["content"] + "\n\n" + policy}
    else:
        result.insert(0, {"role": "system", "content": policy})
    if len(result) > 1 and result[-1]["role"] == "system":
        result[-1] = {**result[-1], "content": result[-1]["content"] + "\n\n" + policy}
    elif len(result) > 1:
        result.append({"role": "system", "content": policy})
    return result
