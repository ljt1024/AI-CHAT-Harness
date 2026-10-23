from types import SimpleNamespace

from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.artifacts import ArtifactEventBus, MAX_ARTIFACT_BYTES, bundle_html
from agentforge.models import AgentDefinition, ToolCall
from agentforge.permissions import ApprovalManager
from agentforge.sandbox.tools import SandboxTool
from agentforge.store import MongoDBStore
from agentforge.tools.fs import Workspace


def test_html_bundle_preserves_document_and_local_dependencies(tmp_path):
    workspace = Workspace(tmp_path)
    folder = tmp_path / "game"
    folder.mkdir()
    (folder / "index.html").write_text('<!doctype html><html lang="zh"><link rel="stylesheet" href="style.css"><body><h1>你好</h1><script src="game.js"></script></body></html>')
    (folder / "style.css").write_text('body{color:green}')
    (folder / "game.js").write_text('document.body.dataset.ready="yes";')
    result = bundle_html(workspace, "game/index.html").decode()
    assert '<html lang="zh">' in result and "你好" in result
    assert "<style>body{color:green}</style>" in result
    assert '<script>document.body.dataset.ready="yes";</script>' in result
    assert 'src="game.js"' not in result


@pytest.mark.parametrize("asset", ["../.env", "../../outside.txt", ".skills/SKILL.md"])
def test_html_bundle_cannot_export_private_assets(tmp_path, asset):
    workspace = Workspace(tmp_path / "work")
    (workspace.root / "index.html").write_text(f'<script src="{asset}"></script>')
    with pytest.raises(ValueError):
        bundle_html(workspace, "index.html")


def test_html_bundle_size_limit_and_symlink_escape(tmp_path):
    workspace = Workspace(tmp_path / "work")
    (workspace.root / "index.html").write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(ValueError):
        bundle_html(workspace, "index.html")
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    (workspace.root / "link.js").symlink_to(outside)
    (workspace.root / "index.html").write_text('<script src="link.js"></script>')
    with pytest.raises(ValueError):
        bundle_html(workspace, "index.html")


@pytest.mark.parametrize("path,allowed", [("game/index.html", True), ("game/style.css", True),
    ("game/game.js", True), ("../outside.html", False), (".env", False),
    (".skills/test/SKILL.md", False), ("secrets/key.txt", False)])
async def test_sandbox_write_rules_without_prompts(tmp_path, path, allowed):
    workspace = Workspace(tmp_path)
    tool = SandboxTool(None, SimpleNamespace(workspace=workspace), "write_file")
    manager = ApprovalManager(MongoDBStore("", "test", client=AsyncMongoMockClient()))
    async def emit(*args, **kwargs):
        pytest.fail("Ordinary writes or invalid paths must not open an approval dialog")
    result = await manager.check(AgentDefinition(name="test"), tool,
        ToolCall(name="write_file", arguments={"path": path}), emit)
    assert result is allowed


async def test_artifact_is_persisted_before_done_and_denied_writes_are_ignored(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "index.html").write_text("<h1>交付</h1>")
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = ArtifactEventBus(None, store, lambda: workspace)
    for identity, path, ok in [("good", "index.html", True), ("bad", "denied.html", False)]:
        await bus.emit({"type": "tool_call", "call": {"id": identity, "name": "write_file",
            "arguments": {"path": path}}})
        await bus.emit({"type": "tool_result", "call_id": identity, "result": {"ok": ok}})
    await bus.emit({"type": "done", "sessionId": "session", "turnId": "turn", "result": {}})
    artifact = bus.events[-2]["artifact"]
    assert bus.events[-1]["result"]["artifacts"] == [artifact]
    assert artifact["toolCallId"] == "good"
    record = await store.get("artifacts", artifact["fileId"])
    assert record["sessionId"] == "session"
    assert "交付" in record["content"].decode()


async def test_css_only_followup_delivers_a_fresh_snapshot(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="style.css"><h1>页面</h1>')
    (tmp_path / "style.css").write_text("h1{color:red}")
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    for color in ("red", "green"):
        (tmp_path / "style.css").write_text(f"h1{{color:{color}}}")
        bus = ArtifactEventBus(None, store, lambda: workspace)
        await bus.emit({"type": "tool_call", "call": {"id": color, "name": "write_file",
            "arguments": {"path": "style.css"}}})
        await bus.emit({"type": "tool_result", "call_id": color, "result": {"ok": True}})
        await bus.emit({"type": "done", "sessionId": "session", "turnId": color, "result": {}})
        assert len(bus.events[-1]["result"]["artifacts"]) == 1
    records = await store.list("artifacts")
    assert len(records) == 2
    assert b"color:red" in records[0]["content"]
    assert b"color:green" in records[1]["content"]
