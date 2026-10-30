import asyncio
import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.gateway.registry import ModelGateway
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


@pytest.mark.parametrize("approved", [True, False])
async def test_truncated_response_recovers_before_approved_docker_write(tmp_path, docker_client, approved):
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            arguments, finish = '{"path":"index.html","content":"unfinished', "length"
        elif len(requests) == 2:
            assert "NONE of its tool calls were executed" in body["messages"][-2]["content"]
            arguments, finish = json.dumps({"path": "index.html", "content": "<!doctype html><title>verified</title>"}), "tool_calls"
        else:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"完成"},"finish_reason":"stop"}]}\n\n'
                                  'data: [DONE]\n\n')
        frames = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"call-{len(requests)}",
            "function": {"name": "write_file", "arguments": arguments}}]}, "finish_reason": finish}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4096 if finish == "length" else 20}}]
        return httpx.Response(200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n")

    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_client:
        gateway = ModelGateway()
        gateway.register("deepseek", DeepSeekAdapter("test-key", model_client))
        app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0), store, gateway)
        async with app.router.lifespan_context(app):
            async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/agents", json={"name": "Explicit write approval",
                    "tools": ["write_file"], "permissions": {"write_file": "confirm"},
                    "sandbox_policy": {}})
                assert created.status_code == 201
                run = asyncio.create_task(client.post("/api/agents/run", json={
                    "sessionId": "recovery", "input": "生成网页", "stream": True,
                    "agentIds": [created.json()["data"]["id"]]}))
                try:
                    for _ in range(500):
                        if app.state.approvals.pending:
                            break
                        if run.done():
                            pytest.fail((await run).text)
                        await asyncio.sleep(0.01)
                    assert len(app.state.approvals.pending) == 1
                    assert len(requests) == 2
                    workspace = app.state.sandbox_pool.workspaces["recovery"].root
                    assert not (workspace / "index.html").exists()
                    approval = next(iter(app.state.approvals.pending))
                    response = await client.post(f"/api/approvals/{approval}", json={"approved": approved})
                    assert response.status_code == 200
                    result = await asyncio.wait_for(run, 10)
                finally:
                    if not run.done():
                        run.cancel()
                        await asyncio.gather(run, return_exceptions=True)
                events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
                assert events[-1]["type"] == "done"
                assert events[-1]["result"]["stop_reason"] == "completed"
                assert events[-1]["result"]["cost"]["tokens_out"] == 4116
                assert sum(e["type"] == "await_approval" for e in events) == 1
                assert any(e.get("step", {}).get("id", "").startswith("recovery:") for e in events)
                assert (workspace / "index.html").exists() is approved
                if approved:
                    assert (workspace / "index.html").read_text() == "<!doctype html><title>verified</title>"
                tool_calls = await store.list("tool_calls")
                assert len(tool_calls) == 1 and tool_calls[0]["result"]["ok"] is approved
