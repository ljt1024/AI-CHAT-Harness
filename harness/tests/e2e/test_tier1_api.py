import asyncio
from contextlib import closing
import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ModelOutputError, ToolCallRequest, UsageReport
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


class WriteThenFinish:
    async def chat_stream(self, req):
        if any(m["role"] == "tool" for m in req.messages):
            yield ContentDelta(text="Task finished.")
        else:
            yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": "result.txt", "content": "verified"}))
        yield UsageReport(tokens_in=10, tokens_out=5, usd=0.00001)
        yield Done()


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("isolated", [False, True])
async def test_create_run_approve_and_trace(tmp_path, approved, isolated):
    if isolated:
        import docker
        from docker.errors import DockerException
        try:
            with closing(docker.from_env(timeout=5)) as docker_client:
                docker_client.ping()
                docker_client.images.get("python:3.12-slim")
        except DockerException as exc:
            pytest.skip(f"Real Docker requires python:3.12-slim: {type(exc).__name__}")
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", WriteThenFinish())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0)
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Write report", "tools": ["write_file"],
                "permissions": {"write_file": "confirm"},
                "sandbox_policy": {} if isolated else None})
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
            workspace = (app.state.sandbox_pool.workspaces[session_id].root
                         if isolated else tmp_path / session_id)
            assert not (workspace / "result.txt").exists()
            resolved = await client.post(f"/api/approvals/{approval_id}", json={"approved": approved})
            assert resolved.status_code == 200
            response = await client.get(f"/api/sessions/{session_id}/events")
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert "await_approval" in [e["type"] for e in events]
            assert events[-1]["type"] == "done"
            assert (workspace / "result.txt").exists() is approved
            if isolated:
                assert not (tmp_path / session_id / "result.txt").exists()
            assert (await store.get("sessions", session_id))["isolated"] is isolated
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
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path), store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/agents/run", json={"input": "hello", "sessionId": "s1", "turnId": "t1", "stream": True})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[0]["type"] == "start"
            assert events[-1]["result"]["output"] == "hello"
            assert all(e["turnId"] == "t1" for e in events)
            completion = await client.post("/api/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
            assert '"content": "hello"' in completion.text
            assert "data: [DONE]" in completion.text


async def test_plain_chat_length_limit_is_a_terminal_actionable_error(tmp_path):
    class Incomplete:
        async def chat_stream(self, req):
            yield ContentDelta(text="partial")
            raise ModelOutputError("output_limit", "length")

    gateway = ModelGateway()
    gateway.register("deepseek", Incomplete())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     MongoDBStore("", "test", client=AsyncMongoMockClient()), gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/chat/completions", json={
                "messages": [{"role": "user", "content": "编写一个很长的页面"}]})
            assert "data: [DONE]" not in result.text
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["error"]["code"] == "output_limit"
            assert "长度限制" in events[-1]["error"]["message"]


@pytest.mark.parametrize("status", [401, 402, 429, 500, None])
async def test_chat_provider_failures_are_terminal_sse_errors(tmp_path, status):
    class Failure:
        async def chat_stream(self, req):
            yield ContentDelta(text="partial")
            if status is None:
                raise httpx.ReadError("private transport diagnostics")
            response = httpx.Response(status, request=httpx.Request("POST", "https://provider.test"))
            raise httpx.HTTPStatusError("private provider diagnostics", request=response.request,
                                        response=response)

    gateway = ModelGateway()
    gateway.register("deepseek", Failure())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/chat/completions", json={
                "messages": [{"role": "user", "content": "hello"}]})
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            assert events[0]["choices"][0]["delta"]["content"] == "partial"
            assert events[-1]["error"]["message"]
            assert "private" not in response.text
            assert "[DONE]" not in response.text
