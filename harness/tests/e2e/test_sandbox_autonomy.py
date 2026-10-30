import json

import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


async def test_multiple_docker_commands_finish_without_human_intervention(tmp_path, docker_client):
    class GenerateAndCheck:
        async def chat_stream(self, req):
            executed = sum(m["role"] == "tool" for m in req.messages)
            commands = [
                "python -c \"from pathlib import Path; Path('index.html').write_text('<h1>自动完成</h1>')\"",
                "python -c \"from pathlib import Path; assert '自动完成' in Path('index.html').read_text(); print('验证通过')\"",
                "wc -c index.html",
            ]
            if executed < len(commands):
                yield ToolCallRequest(call=ToolCall(name="run_command", arguments={"command": commands[executed]}))
            else:
                yield ContentDelta(text="已自动生成并验证 HTML。")
            yield Done()

    gateway = ModelGateway()
    gateway.register("deepseek", GenerateAndCheck())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/agents/run", json={"input": "生成并验证HTML",
                "sessionId": "autonomous-commands", "sandbox": True, "stream": True})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["type"] == "done"
            assert events[-1]["result"]["stop_reason"] == "completed"
            assert not any(e["type"] == "await_approval" for e in events)
            assert not await store.list("approvals")
            calls = await store.list("tool_calls")
            assert len(calls) == 3 and all(c["result"]["ok"] for c in calls)
            artifact = events[-1]["result"]["artifacts"][0]
            downloaded = await client.get(artifact["downloadPath"])
            assert downloaded.status_code == 200 and "自动完成" in downloaded.text
            assert not (tmp_path / "autonomous-commands" / "index.html").exists()
