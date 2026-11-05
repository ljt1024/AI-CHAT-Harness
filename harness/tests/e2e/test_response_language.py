import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.prompts import RESPONSE_LANGUAGE_POLICY
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


@pytest.mark.parametrize("agent_mode", [False, True])
@pytest.mark.parametrize("prompt", ["现在有哪些技能？", "请用英文回答：现在有哪些技能？",
                                   "What skills are available?"])
async def test_language_policy_reaches_every_model_call(tmp_path, agent_mode, prompt):
    requests = []

    class Capture:
        async def chat_stream(self, req):
            requests.append(req)
            assert req.messages[0]["role"] == "system"
            assert RESPONSE_LANGUAGE_POLICY in req.messages[0]["content"]
            assert req.messages[-1]["role"] == "system"
            assert RESPONSE_LANGUAGE_POLICY in req.messages[-1]["content"]
            assert {"role": "user", "content": prompt} in req.messages
            if agent_mode and not any(m["role"] == "tool" for m in req.messages):
                yield ToolCallRequest(call=ToolCall(name="list_skills", arguments={}))
            else:
                yield ContentDelta(text="answer")
            yield Done()

    gateway = ModelGateway()
    gateway.register("deepseek", Capture())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    history = [{"role": "user", "content": "What can you do?"},
               {"role": "assistant", "content": "I can help you install skills."}]
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # A resumed conversation belongs to the account that owns it, so the
            # fixture creates it as that account rather than anonymously.
            owner = (await client.get("/api/auth/me")).json()["data"]["id"]
            await store.put("sessions", {"id": "language-test", "history": history}, owner=owner)
            if agent_mode:
                response = await client.post("/api/agents/run", json={
                    "sessionId": "language-test", "input": prompt, "stream": True})
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                assert events[-1]["type"] == "done"
                assert len(requests) == 2
                trace = (await store.list("traces", {"sessionId": "language-test"}))[0]
                turns = sorted(await store.list("turns", {"trace_id": trace["id"]}), key=lambda t: t["index"])
                for turn, request in zip(turns, requests, strict=True):
                    assert turn["model_messages"] == request.messages
                    assert "list_skills" in turn["enabled_tools"]
                stored = await store.get("sessions", "language-test")
                assert not any(m["role"] == "system" for m in stored["history"])
            else:
                response = await client.post("/api/chat/completions", json={
                    "messages": [*history, {"role": "user", "content": prompt}]})
                assert "data: [DONE]" in response.text
                assert len(requests) == 1
            assert response.status_code == 200
