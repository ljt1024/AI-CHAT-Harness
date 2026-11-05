from io import BytesIO
import json
from types import SimpleNamespace
import zipfile

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.artifacts import (MAX_ARTIFACT_BYTES, ArtifactEventBus, bundle_html,
                                  read_document)
from agentforge.config import Settings
from agentforge.gateway.registry import ModelGateway
from agentforge.models import AgentDefinition, ToolCall
from agentforge.permissions import ApprovalManager
from agentforge.previews import build_preview
from agentforge.sandbox.tools import SandboxTool
from agentforge.store import MongoDBStore
from agentforge.tools.fs import Workspace
from helpers import AuthedClient, TEST_PASSWORD

SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PACKAGES = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def package(members):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def make_xlsx():
    """A two-sheet workbook whose first row names the columns."""
    return package({
        "xl/workbook.xml": f'<workbook xmlns="{SPREADSHEET}" xmlns:r="{OFFICE}"><sheets>'
                           f'<sheet name="季度" sheetId="1" r:id="rId1"/>'
                           f'<sheet name="明细" sheetId="2" r:id="rId2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PACKAGES}">'
                                      f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                                      f'<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/>'
                                      f'</Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{SPREADSHEET}"><si><t>城市</t></si>'
                                f'<si><t>销量</t></si><si><t>上海</t></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{SPREADSHEET}"><sheetData>'
                                    f'<row r="1"><c r="A1" t="s"><v>0</v></c>'
                                    f'<c r="B1" t="s"><v>1</v></c></row>'
                                    f'<row r="2"><c r="A2" t="s"><v>2</v></c>'
                                    f'<c r="B2"><v>42</v></c></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{SPREADSHEET}"><sheetData>'
                                    f'<row r="1"><c r="A1" t="inlineStr"><is><t>编号</t></is></c>'
                                    f'</row></sheetData></worksheet>',
    })


def slide(title, body):
    return (f'<p:sld xmlns:p="{PRESENTATION}" xmlns:a="{DRAWING}"><p:cSld><p:spTree>'
            f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody>'
            f'<a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
            f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody>'
            f'<a:p><a:pPr lvl="1"/><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>'
            f'</p:spTree></p:cSld></p:sld>')


def make_pptx():
    # Deliberately out of alphabetical order: slide 10 must follow slide 2.
    return package({"ppt/slides/slide2.xml": slide("第二页", "要点二"),
                    "ppt/slides/slide10.xml": slide("第十页", "要点十"),
                    "ppt/slides/slide1.xml": slide("第一页", "要点一")})


def make_docx():
    return package({"word/document.xml": f'<w:document xmlns:w="{WORD}"><w:body>'
                                         f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                                         f'<w:r><w:t>标题</w:t></w:r></w:p>'
                                         f'<w:p><w:r><w:t>正文</w:t></w:r></w:p>'
                                         f'</w:body></w:document>'})


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


def test_sheets_preview_reads_shared_inline_and_numeric_cells():
    kind, payload = build_preview("xlsx", make_xlsx())
    assert kind == "sheets"
    first, second = payload["sheets"]
    # The relationship targets are written two ways by real writers: relative and
    # rooted, and both have to resolve.
    assert first["name"] == "季度" and second["name"] == "明细"
    assert first["columns"] == ["城市", "销量"]
    assert first["rows"] == [["上海", "42"]]
    assert second["columns"] == [] and second["rows"] == [["编号"]]


def test_slides_preview_follows_numeric_slide_order():
    kind, payload = build_preview("pptx", make_pptx())
    assert kind == "slides"
    assert [slide["title"] for slide in payload["slides"]] == ["第一页", "第二页", "第十页"]
    assert payload["slides"][0]["body"] == ["  要点一"]


def test_document_preview_keeps_heading_styles():
    kind, payload = build_preview("docx", make_docx())
    assert kind == "document"
    assert payload["paragraphs"] == [{"style": "Heading1", "text": "标题"},
                                     {"style": "", "text": "正文"}]


def test_a_file_that_is_not_the_format_it_claims_is_rejected():
    # An agent can name a file anything; a PNG called .pptx must fail as a failed
    # delivery, never as an exception out of the turn.
    with pytest.raises(ValueError):
        build_preview("pptx", b"not a zip package at all")


async def run_turn(workspace, store):
    bus = ArtifactEventBus(None, store, lambda: workspace)
    await bus.emit({"type": "tool_call", "call": {"id": "cmd", "name": "run_command",
                                                  "arguments": {}}})
    await bus.emit({"type": "tool_result", "call_id": "cmd", "result": {"ok": True}})
    await bus.emit({"type": "done", "sessionId": "session", "turnId": "turn", "result": {}})
    return bus


async def test_workspace_documents_are_delivered_with_previews(tmp_path):
    """A rendered PDF or a built deck used to stay in the workspace, unreachable."""
    workspace = Workspace(tmp_path)
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4\n% a real one is not needed to deliver it")
    (tmp_path / "deck.pptx").write_bytes(make_pptx())
    (tmp_path / "book.xlsx").write_bytes(make_xlsx())
    (tmp_path / "note.docx").write_bytes(make_docx())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = await run_turn(workspace, store)
    delivered = {item["fileName"]: item for item in bus.events[-1]["result"]["artifacts"]}
    assert set(delivered) == {"report.pdf", "deck.pptx", "book.xlsx", "note.docx"}
    assert delivered["report.pdf"]["format"] == "pdf"
    assert delivered["report.pdf"]["mimeType"] == "application/pdf"
    assert "previewFileId" not in delivered["report.pdf"]
    for name, kind in [("deck.pptx", "slides"), ("book.xlsx", "sheets"), ("note.docx", "document")]:
        assert delivered[name]["previewKind"] == kind
        record = await store.get("artifacts", delivered[name]["previewFileId"])
        assert record["delivery"] == "preview"
        assert json.loads(bytes(record["content"]))
    # Every artifact reached the browser as its own event.
    assert sum(1 for event in bus.events if event["type"] == "artifact") == 4


async def test_an_unchanged_file_is_delivered_once(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "deck.pptx").write_bytes(make_pptx())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    first = (await run_turn(workspace, store)).events[-1]["result"]["artifacts"]
    second = (await run_turn(workspace, store)).events[-1]["result"]["artifacts"]
    # The card still appears under the newer turn, but the bytes are stored once.
    assert [item["fileId"] for item in first] == [item["fileId"] for item in second]
    stored = [record for record in await store.list("artifacts")
              if record.get("delivery") != "preview"]
    assert len(stored) == 1


async def test_a_file_past_the_limit_is_reported_and_skipped(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "huge.pdf").write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = await run_turn(workspace, store)
    assert bus.events[-1]["result"]["artifacts"] == []
    failed = [event["step"] for event in bus.events if event["type"] == "step"]
    assert failed and "huge.pdf" in failed[-1]["output"]


async def test_a_broken_office_file_only_fails_its_own_delivery(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "deck.pptx").write_bytes(b"definitely not a package")
    (tmp_path / "note.docx").write_bytes(make_docx())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = await run_turn(workspace, store)
    delivered = [item["fileName"] for item in bus.events[-1]["result"]["artifacts"]]
    assert delivered == ["note.docx"]


async def test_image_deliveries_are_capped(tmp_path):
    """Fifty generated charts must not crowd the deck itself out of the delivery."""
    workspace = Workspace(tmp_path)
    for index in range(9):
        (tmp_path / f"chart{index}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "deck.pptx").write_bytes(make_pptx())
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = await run_turn(workspace, store)
    formats = [item["format"] for item in bus.events[-1]["result"]["artifacts"]]
    assert formats.count("image") == 6
    assert "pptx" in formats


async def test_hidden_and_dependency_folders_are_not_delivered(tmp_path):
    workspace = Workspace(tmp_path)
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "secret.pdf").write_bytes(b"%PDF-1.4")
    nested = tmp_path / "site" / "node_modules"
    nested.mkdir(parents=True)
    (nested / "chart.png").write_bytes(b"\x89PNG")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    bus = await run_turn(workspace, store)
    assert [item["fileName"] for item in bus.events[-1]["result"]["artifacts"]] == ["report.pdf"]


def test_read_document_refuses_a_file_past_the_limit(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "big.pdf").write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(ValueError):
        read_document(workspace, "big.pdf")


async def test_download_sends_the_real_content_type(tmp_path):
    """Every delivery used to arrive as text/html; a PDF that lies about its type
    cannot be rendered, and an HTML one that does not get the sandbox cannot be
    opened safely."""
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    app = create_app(Settings(_env_file=None, admin_password=TEST_PASSWORD, agentforge_workspace=tmp_path, sandbox_warm_size=0),
                     store, ModelGateway())
    async with app.router.lifespan_context(app):
        async with AuthedClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            # Deliveries belong to a session, and a download is authorized through
            # it, so the fixture creates both as the logged-in account.
            owner = (await client.get("/api/auth/me")).json()["data"]["id"]
            await store.put("sessions", {"id": "session"}, owner=owner)
            for identity, mime, body in [("pdf-1", "application/pdf", b"%PDF-1.4"),
                                         ("html-1", "text/html", b"<h1>hi</h1>"),
                                         ("png-1", "image/png", b"\x89PNG")]:
                await store.put("artifacts", {"id": identity, "sessionId": "session",
                    "artifact": {"fileId": identity, "fileName": f"{identity}.bin",
                                 "format": "pdf", "mimeType": mime, "size": len(body)},
                    "content": body, "sha256": identity}, owner=owner)
            pdf = await client.get("/api/files/pdf-1/download")
            html = await client.get("/api/files/html-1/download")
            image = await client.get("/api/files/png-1/download")
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert image.headers["content-type"].startswith("image/png")
    assert "content-security-policy" not in pdf.headers
    assert html.headers["content-type"].startswith("text/html")
    assert "sandbox" in html.headers["content-security-policy"]
