"""Test-only browser fixture: deterministic model, mock DB, real Docker/approval API."""
import asyncio
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory

from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.store import MongoDBStore


class ApprovalFixture:
    async def chat_stream(self, request):
        task = next(m["content"] for m in reversed(request.messages) if m["role"] == "user")
        previous = [m for m in request.messages if m["role"] == "tool"]
        count = 2 if "queue" in task else 1
        if len(previous) < count:
            await asyncio.sleep(0.15)
            yield ToolCallRequest(call=ToolCall(name="write_file", arguments={
                "path": f"result-{len(previous)}.txt", "content": "中文参数 " * 1500}))
        else:
            yield ContentDelta(text="审批流程已完成。")
        yield Done()


workspace = TemporaryDirectory(prefix="approval-composer-")
store = MongoDBStore("", "test", client=AsyncMongoMockClient())
gateway = ModelGateway()
gateway.register("deepseek", ApprovalFixture())
app = create_app(Settings(_env_file=None, agentforge_workspace=workspace.name,
    sandbox_warm_size=0, approval_timeout=12), store, gateway)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        default = await store.get("agents", "default")
        await store.put("agents", {**default, "permissions": {"write_file": "confirm"}})
        yield


app.router.lifespan_context = lifespan
