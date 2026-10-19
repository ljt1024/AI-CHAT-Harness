import asyncio
import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore


class WriteThenFinish:
    async def chat_stream(self, req):
        if any(m["role"] == "tool" for m in req.messages):
            yield ContentDelta(text="Task finished.")
        else:
            yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": "result.txt", "content": "verified"}))
        yield UsageReport(tokens_in=10, tokens_out=5, usd=0.00001)
        yield Done()


@pytest.mark.parametrize("approved", [True, False])
async def test_create_run_approve_and_trace(tmp_path, approved):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", WriteThenFinish())
    settings = Settings(_env_file=None, agentforge_workspace=tmp_path)
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Write report", "tools": ["write_file"],
                "permissions": {"write_file": "confirm"}})
            assert created.status_code == 201
            agent_id = created.json()["data"]["id"]
            started = await client.post("/api/sessions", json={"agent_id": agent_id, "task": "Write result"})
            assert started.status_code == 201
            session_id = started.json()["session_id"]
            for _ in range(100):
                if app.state.approvals.pending:
                    break
                await asyncio.sleep(0.01)
            approval_id = next(iter(app.state.approvals.pending))
            resolved = await client.post(f"/api/approvals/{approval_id}", json={"approved": approved})
            assert resolved.status_code == 200
            response = await client.get(f"/api/sessions/{session_id}/events")
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert "await_approval" in [e["type"] for e in events]
            assert events[-1]["type"] == "done"
            assert (tmp_path / session_id / "result.txt").exists() is approved
            trace = (await client.get(f"/api/traces/{session_id}")).json()["traces"][0]
            assert len(trace["turns"]) == 2
            assert trace["turns"][0]["middleware"]["middleware"]
            assert trace["cost"]["tokens_in"] == 20
            assert trace["tool_calls"][0]["result"]["ok"] is approved


async def test_legacy_and_plain_chat(tmp_path):
    from agentforge.gateway.base import ChatRequest
    class Answer:
        async def chat_stream(self, req: ChatRequest):
            yield ContentDelta(text="hello")
            yield Done()
    gateway = ModelGateway()
    gateway.register("deepseek", Answer())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/agents/run", json={"input": "hello", "sessionId": "s1", "turnId": "t1", "stream": True})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[0]["type"] == "start"
            assert events[-1]["result"]["output"] == "hello"
            assert all(e["turnId"] == "t1" for e in events)
            completion = await client.post("/api/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
            assert '"content": "hello"' in completion.text
            assert "data: [DONE]" in completion.text
