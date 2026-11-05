import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore
from helpers import AuthedClient, TEST_PASSWORD


class GenerateHtml:
    async def chat_stream(self, req):
        if any(m["role"] == "tool" for m in req.messages):
            yield ContentDelta(text="已生成 HTML，可以预览和下载。")
        else:
            for path, content in [("site/index.html", '<h1>测试页面</h1><link rel="stylesheet" href="style.css"><script src="app.js"></script>'),
                                  ("site/style.css", 'h1{color:green}'),
                                  ("site/app.js", 'document.body.dataset.ready="yes";')]:
                yield ToolCallRequest(call=ToolCall(name="write_file", arguments={"path": path, "content": content}))
        yield Done()


@pytest.mark.parametrize("isolated", [False, True])
async def test_html_delivery_without_approval_and_survives_restart(tmp_path, isolated, request):
    if isolated:
        request.getfixturevalue("docker_client")
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", GenerateHtml())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0)
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/agents/run", json={"input": "生成一个HTML", "sessionId": "html-session", "stream": True, "sandbox": isolated})
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert not any(e["type"] == "await_approval" for e in events)
            assert events[-1]["type"] == "done"
            artifact = next(e["artifact"] for e in events if e["type"] == "artifact")
            assert events[-1]["result"]["artifacts"] == [artifact]
            response = await client.get(artifact["downloadPath"])
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.headers["content-disposition"].startswith("attachment;")
            assert response.headers["content-security-policy"].startswith("sandbox;")
            assert 'h1{color:green}' in response.text
            assert 'document.body.dataset.ready="yes"' in response.text
            assert (await client.get("/api/files/unknown/download")).status_code == 404
    # A new app/process can still serve the immutable delivery after its container closes.
    app = create_app(settings, store, gateway)
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            persisted = await client.get(artifact["downloadPath"])
            assert persisted.content == response.content
