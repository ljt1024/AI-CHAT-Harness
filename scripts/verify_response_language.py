"""Real-model language regression, including progress text and English skill instructions.

Run from harness with an existing conversation:
uv run python ../scripts/verify_response_language.py --history-session <id>
"""
import argparse
import asyncio
import json
import re
from uuid import uuid4

import httpx

from agentforge.config import Settings
from agentforge.prompts import RESPONSE_LANGUAGE_POLICY
from agentforge.store import MongoDBStore


def assert_language(text, chinese):
    prose = re.sub(r"```.*?```|`[^`]*`|https?://\S+", "", text, flags=re.DOTALL).strip()
    if not prose:
        return
    first_paragraph = next(part.strip() for part in prose.split("\n\n") if part.strip())
    has_chinese = bool(re.search(r"[\u3400-\u9fff]", first_paragraph))
    assert has_chinese is chinese, f"Wrong language in streamed response: {first_paragraph[:300]}"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-session", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:5196")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    settings = Settings()
    store = MongoDBStore(settings.mongodb_uri, settings.agentforge_db)
    try:
        source = await store.get("sessions", args.history_session)
        if not source:
            raise ValueError("Source conversation not found")
        cases = [
            ("skill progress", "使用frontend-design这个技能，操作前先说明你准备做什么，再读取技能说明，"
             "用三句话介绍如何设计HTML。不要创建文件或执行命令。", True),
            ("HTML design", "使用frontend-design这个技能帮我实现一个小丑扔球的html。开始前先说明思路，"
             "再给出不超过30行的HTML示例。不要写入文件或执行命令。", True),
        ] * args.repeats
        cases += [
            ("explicit English", "请用英文回答：用一句话解释frontend-design技能的作用。不要调用工具。", False),
            ("translation", "将‘安装成功’翻译成英文，只输出译文。不要调用工具。", False),
        ]
        async with httpx.AsyncClient(base_url=args.url, timeout=180, trust_env=False) as client:
            for label, prompt, chinese in cases:
                identity = "language-verify-" + uuid4().hex
                try:
                    await store.put("sessions", {"id": identity, "isolated": True,
                        "history": source.get("history", []), "loaded_skills": source.get("loaded_skills", [])})
                    events = []
                    async with client.stream("POST", "/api/agents/run", json={
                        "sessionId": identity, "input": prompt, "sandbox": True, "stream": True}) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            event = json.loads(line[6:])
                            events.append(event)
                            if event["type"] == "await_approval":
                                decision = await client.post(f"/api/approvals/{event['call_id']}",
                                                             json={"approved": False})
                                decision.raise_for_status()
                            assert event["type"] != "error", event
                    assert events[-1]["type"] == "done", events[-1]
                    result = events[-1]["result"]
                    assert result["stop_reason"] == "completed", result
                    assert_language(result["output"], chinese)
                    turns = sorted(await store.list("turns", {"trace_id": result["id"]}), key=lambda t: t["index"])
                    response_count = 0
                    for turn in turns:
                        actual = turn["model_messages"]
                        assert RESPONSE_LANGUAGE_POLICY in actual[0]["content"]
                        assert RESPONSE_LANGUAGE_POLICY in actual[-1]["content"]
                        text = "".join(c["text"] for c in turn["chunks"] if c["type"] == "content")
                        if text.strip():
                            assert_language(text, chinese)
                            response_count += 1
                    assert response_count
                    print(f"PASS: {label}, {response_count} streamed response(s) checked including pre-tool text", flush=True)
                finally:
                    await store.db.sessions.delete_one({"id": identity})
    finally:
        store.client.close()


if __name__ == "__main__":
    asyncio.run(main())
