"""Verify a natural installation request with a copy of stale conversation history.

Run from harness: uv run python ../scripts/verify_skill_history.py --session-id <id>
"""
import argparse
import asyncio
import json
from uuid import uuid4

import httpx

from agentforge.config import Settings
from agentforge.store import MongoDBStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8026")
    args = parser.parse_args()
    settings = Settings()
    store = MongoDBStore(settings.mongodb_uri, settings.agentforge_db)
    session_id = "history-verify-" + uuid4().hex
    name = "history-verify-" + uuid4().hex[:8]
    installed_id = None
    original = await store.get("sessions", args.session_id)
    if not original or not original.get("history"):
        raise ValueError("Source conversation must have history.")
    try:
        await store.put("sessions", {"id": session_id, "history": original["history"],
                                    "isolated": original.get("isolated", False)})
        async with httpx.AsyncClient(base_url=args.url, timeout=120, trust_env=False) as client:
            for approved in (False, True):
                approvals, installed, completed = 0, False, False
                prompt = ("帮我安装这个技能：https://github.com/anthropics/skills/tree/main/skills/frontend-design，"
                          f"名称使用 {name}。只安装，不加载或执行。如果审批被拒绝就停止。")
                async with client.stream("POST", "/api/agents/run", json={
                    "input": prompt, "sessionId": session_id, "stream": True,
                    "sandbox": original.get("isolated", False)}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["type"] == "await_approval":
                            assert event["tool"] == "install_skill", event
                            assert event["arguments"]["source"] == "github", event
                            assert approvals == 0, "Unexpected repeated approval"
                            approvals += 1
                            decision = await client.post(f"/api/approvals/{event['call_id']}",
                                                         json={"approved": approved})
                            decision.raise_for_status()
                        elif event["type"] == "skill_installed":
                            installed_id = event["skill_id"]
                            installed = True
                        elif event["type"] == "error":
                            raise AssertionError(event)
                        elif event["type"] == "done":
                            completed = True
                assert approvals == 1, "Model did not request installation approval"
                assert completed and installed is approved
                package = await store.db.skills.find_one({"name": name})
                assert bool(package) is approved
                if approved:
                    assert package["provenance"]["source"] == "github"
                    assert len(package["provenance"]["commit"]) == 40
                print(f"PASS: stale history + natural Chinese request, approved={approved}, installed={installed}",
                      flush=True)
        assert await store.get("sessions", args.session_id) == original, "Original session changed"
    finally:
        if installed_id:
            await store.db.skills.delete_one({"id": installed_id, "name": name})
        await store.db.sessions.delete_one({"id": session_id})
        store.client.close()


if __name__ == "__main__":
    asyncio.run(main())
