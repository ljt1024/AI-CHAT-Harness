import asyncio
from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from agentforge.events import EventBus
from agentforge.models import AgentDefinition, ToolCall
from agentforge.permissions import ApprovalManager
from agentforge.sandbox.tools import SandboxTool
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


@pytest.mark.parametrize("command", ["pwd", "python check.py", "python .skills/demo/scripts/run.py"])
async def test_docker_commands_run_without_confirmation(tmp_path, command):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = ApprovalManager(store)
    box = SimpleNamespace(workspace=Workspace(tmp_path), alive=True)
    tool = SandboxTool(None, box, "run_command")
    async def emit(*args, **kwargs):
        pytest.fail("Default Docker commands must not prompt")
    assert await manager.check(AgentDefinition(name="auto"), tool,
        ToolCall(name="run_command", arguments={"command": command}), emit)
    assert not manager.pending and not await store.list("approvals")


async def test_stopped_docker_is_not_automatically_authorized(tmp_path):
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()))
    tool = SandboxTool(None, SimpleNamespace(workspace=Workspace(tmp_path), alive=False), "run_command")
    async def emit(*args, **kwargs):
        pytest.fail("A stopped sandbox must fail closed")
    assert not await manager.check(AgentDefinition(name="auto"), tool,
        ToolCall(name="run_command", arguments={"command": "pwd"}), emit)


async def test_docker_explicit_confirmation_cannot_be_downgraded(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager, emitted = ApprovalManager(store, timeout=0.01), []
    tool = SandboxTool(None, SimpleNamespace(workspace=Workspace(tmp_path), alive=True), "run_command")
    async def emit(kind, **kwargs):
        emitted.append(kind)
    assert not await manager.check(AgentDefinition(name="strict", permissions={"run_command": "confirm"}),
        tool, ToolCall(name="run_command", arguments={"command": "pwd"}), emit)
    assert emitted == ["await_approval", "approval_resolved"]


async def test_other_tools_named_run_command_do_not_inherit_docker_autonomy(tmp_path):
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()), timeout=0.01)
    host = ShellTool(Workspace(tmp_path))
    host.permission = "rule"
    async def emit(*args, **kwargs):
        pass
    assert not await manager.check(AgentDefinition(name="test"), host,
        ToolCall(name="run_command", arguments={"command": "pwd"}), emit)
