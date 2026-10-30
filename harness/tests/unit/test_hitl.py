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


def remote_tool(name: str, read_only: bool, destructive: bool = False):
    """A remote tool the way a hosted MCP service describes itself."""
    from agentforge.mcp_client import MCPTool
    definition = SimpleNamespace(name=name, description="remote", inputSchema={"type": "object"},
        annotations=SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive))
    return MCPTool("http://127.0.0.1:9/mcp", definition)


async def test_a_read_only_remote_tool_does_not_ask():
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()))
    async def emit(*args, **kwargs):
        pytest.fail("A read-only remote read must not prompt")
    assert await manager.check(AgentDefinition(name="auto"), remote_tool("fetch", read_only=True),
        ToolCall(name="fetch", arguments={"url": "https://example.com"}), emit, session_id="s1")


@pytest.mark.parametrize("read_only,destructive", [(False, False), (True, True)])
async def test_anything_that_can_change_state_still_asks(read_only, destructive):
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()), timeout=0.01)
    emitted = []
    async def emit(kind, **kwargs):
        emitted.append(kind)
    assert not await manager.check(AgentDefinition(name="auto"),
        remote_tool("write_page", read_only=read_only, destructive=destructive),
        ToolCall(name="write_page"), emit, session_id="s1")
    assert emitted == ["await_approval", "approval_resolved"]


async def test_allow_for_session_stops_asking_for_that_tool_only():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager, asked = ApprovalManager(store, timeout=0.05), []

    async def approve_remembering(kind, **data):
        asked.append((kind, data.get("tool")))
        if kind == "await_approval":
            assert data["rememberable"] is True
            manager.resolve(data["call_id"], True, "", remember=True)

    tool, agent = remote_tool("write_page", read_only=False), AgentDefinition(name="auto")
    assert await manager.check(agent, tool, ToolCall(name="write_page"), approve_remembering, "s1")

    async def quiet(kind, **data):
        pytest.fail("A remembered tool must not ask again in this session")

    assert await manager.check(agent, tool, ToolCall(name="write_page"), quiet, "s1")
    # Another conversation starts from scratch, and a tool it never granted still asks.
    assert await manager.check(agent, tool, ToolCall(name="write_page"), approve_remembering, "s2")
    other = remote_tool("delete_page", read_only=False)
    assert await manager.check(agent, other, ToolCall(name="delete_page"), approve_remembering, "s2")


async def test_host_shell_cannot_be_granted_for_the_session(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = ApprovalManager(store, timeout=0.05)

    async def remember_anyway(kind, **data):
        if kind == "await_approval":
            assert data["rememberable"] is False
            manager.resolve(data["call_id"], True, "", remember=True)

    tool = ShellTool(Workspace(tmp_path))
    tool.permission = "confirm"
    agent = AgentDefinition(name="auto")
    assert await manager.check(agent, tool, ToolCall(name="run_command", arguments={"command": "pwd"}),
                               remember_anyway, "s1")
    # The client asked for a session grant; the gate refused to widen the boundary.
    assert "run_command" not in manager.remembered.get("s1", set())
