import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient

from agentforge.events import EventBus
from agentforge.models import AgentDefinition, ToolCall
from agentforge.permissions import ApprovalManager
from agentforge.store import MongoDBStore
from agentforge.tools.fs import FileTool, Workspace
from agentforge.tools.shell import ShellTool


@pytest.mark.parametrize("approved", [True, False, None])
async def test_approval_denial_timeout(tmp_path, approved):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager, events = ApprovalManager(store, timeout=0.05), EventBus()
    async def emit(kind, **data):
        await events.emit({"type": kind, **data})
    task = asyncio.create_task(manager.check(AgentDefinition(name="test"), ShellTool(Workspace(tmp_path)),
        ToolCall(name="run_command", arguments={"command": "pwd"}), emit))
    async for event in events.stream():
        if event["type"] == "await_approval":
            if approved is not None:
                manager.resolve(event["call_id"], approved)
            break
    assert await task is (approved is True)
    assert not manager.pending
    assert (await store.list("approvals"))[0]["status"] == ("approved" if approved else "denied")


async def test_rules_and_no_permission_downgrade(tmp_path):
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()), timeout=0.01)
    agent = AgentDefinition(name="test", permissions={"run_command": "auto"})
    async def emit(*args, **kwargs):
        pass
    assert not await manager.check(agent, ShellTool(Workspace(tmp_path)), ToolCall(name="run_command"), emit)
    write = FileTool(Workspace(tmp_path), "write_file")
    assert await manager.check(agent, write, ToolCall(name="write_file", arguments={"path": "a"}), emit)
    assert not await manager.check(agent, write, ToolCall(name="write_file", arguments={"path": "../a"}), emit)
