"""Uploads are the one endpoint that writes a request body to disk.

Everything here is an attack or an edge: a name that is a path, a "PNG" that is
not, a body that lies about its length, a file of another account, a session id
that would escape the store, a symlink the agent planted where the copy lands.
"""

import os
from pathlib import Path

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge import session_files
from agentforge.config import Settings
from agentforge.gateway.registry import ModelGateway
from agentforge.session_files import SessionFiles, UploadRejected
from agentforge.store import MongoDBStore
from helpers import TEST_PASSWORD

SESSION = "5f1a2b3c-0000-4000-8000-000000000001"
CSV = "region,total\nnorth,12\n"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class FakeUpload:
    """The part of Starlette's UploadFile this module touches."""

    def __init__(self, filename, chunks):
        self.filename = filename
        self._chunks = list(chunks)

    async def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""


# --------------------------------------------------------------------- naming
def test_a_path_in_the_name_never_becomes_a_path():
    assert session_files.display_name("../../etc/passwd.csv") == "passwd.csv"
    assert session_files.display_name("/etc/passwd.csv") == "passwd.csv"
    assert session_files.display_name(r"C:\Windows\system32\evil.csv") == "evil.csv"
    assert session_files.display_name(".env.csv") == "env.csv"
    assert session_files.display_name("...") == "file"


def test_control_characters_and_length_are_bounded():
    assert session_files.display_name("a\r\nb\t.csv") == "ab.csv"
    long = session_files.display_name("x" * 400 + ".csv")
    assert len(long) < 100 and long.endswith(".csv")


def test_two_files_with_one_name_get_two_labels():
    taken: set[str] = set()
    first = session_files.label_for("report (final).csv", taken)
    taken.add(first)
    second = session_files.label_for("report (final).csv", taken)
    assert first != second
    # Whatever the label is, it is a single path segment with no traversal in it.
    for label in (first, second):
        assert "/" not in label and ".." not in label


def test_a_session_id_cannot_escape_the_store():
    for bad in ("../x", "a/b", "..", "", "a" * 65, "sess ao"):
        with pytest.raises(UploadRejected):
            session_files.session_identity(bad)
    assert session_files.session_identity(SESSION) == SESSION


# --------------------------------------------------------------------- content
def test_an_extension_outside_the_allowlist_is_refused():
    store = SessionFiles(Path("/tmp/unused"), 1024, 5, 4096)
    with pytest.raises(UploadRejected) as caught:
        __import__("asyncio").run(store.save(
            "s", FakeUpload("setup.sh", [b"#!/bin/sh\nrm -rf /\n"]), 1024, set()))
    assert caught.value.status == 415


def test_the_bytes_have_to_match_the_name():
    """A `.png` that starts with a shell script is refused, not stored."""
    store = SessionFiles(Path("/tmp/unused"), 1024, 5, 4096)
    with pytest.raises(UploadRejected) as caught:
        __import__("asyncio").run(store.save(
            "s", FakeUpload("photo.png", [b"#!/bin/sh\n"]), 1024, set()))
    assert caught.value.status == 422


def test_a_stream_that_passes_the_budget_is_cut_off_and_leaves_nothing(tmp_path):
    import asyncio
    store = SessionFiles(tmp_path, 10, 5, 4096)
    upload = FakeUpload("data.csv", [b"a" * 8, b"b" * 8])
    with pytest.raises(UploadRejected) as caught:
        asyncio.run(store.save("s", upload, 10, set()))
    assert caught.value.status == 413
    # The partial file must not survive the refusal.
    assert list((tmp_path / "s").iterdir()) == []


def test_an_empty_body_is_refused(tmp_path):
    import asyncio
    store = SessionFiles(tmp_path, 1024, 5, 4096)
    with pytest.raises(UploadRejected):
        asyncio.run(store.save("s", FakeUpload("data.csv", []), 1024, set()))


# ---------------------------------------------------------------------- copy
def test_the_copy_into_the_workspace_never_follows_a_planted_symlink(tmp_path):
    """The agent can write to the workspace, so it can plant the destination."""
    root = tmp_path / "blobs"
    store = SessionFiles(root, 1024, 5, 4096)
    import asyncio
    record = asyncio.run(store.save(
        "s", FakeUpload("notes.csv", [CSV.encode()]), 1024, set()))
    outside = tmp_path / "outside.txt"
    outside.write_text("originally elsewhere\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "uploads").mkdir(parents=True)
    os.symlink(outside, workspace / "uploads" / record["label"])

    manifest = store.sync_into([{**record, "sessionId": "s"}], workspace)

    destination = workspace / "uploads" / record["label"]
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == CSV
    assert outside.read_text(encoding="utf-8") == "originally elsewhere\n"
    assert manifest[0]["path"] == f"uploads/{record['label']}"


def test_the_prompt_names_the_files_so_the_model_does_not_deny_them():
    manifest = [{"name": "报告.csv", "path": "uploads/report.csv", "size": 2048,
                 "mimeType": "text/csv"}]
    text = session_files.manifest_prompt(manifest)
    assert "uploads/report.csv" in text and "报告.csv" in text
    assert "read_file" in text
    assert session_files.manifest_prompt([]) == ""


# ----------------------------------------------------------------------- api
async def make_app(tmp_path, **overrides):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    settings = Settings(_env_file=None, admin_password=TEST_PASSWORD,
                        agentforge_workspace=tmp_path / "workspaces",
                        sandbox_warm_size=0, sandbox_max_size=1, **overrides)
    return create_app(settings, store, ModelGateway()), store


@pytest.fixture
async def service(tmp_path):
    app, store = await make_app(tmp_path)
    async with app.router.lifespan_context(app):
        yield app, store, tmp_path


def client_for(app):
    from helpers import AuthedClient
    return AuthedClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def upload(client, name="data.csv", body=CSV.encode(), session=SESSION):
    return await client.post(f"/api/sessions/{session}/files",
                             files={"file": (name, body, "text/csv")})


async def test_an_uploaded_file_is_listed_and_downloaded_as_an_attachment(service):
    app, _, _ = service
    async with client_for(app) as client:
        created = await upload(client)
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        assert data["name"] == "data.csv" and data["size"] == len(CSV)

        listed = await client.get(f"/api/sessions/{SESSION}/files")
        assert [row["fileId"] for row in listed.json()["data"]] == [data["fileId"]]

        fetched = await client.get(data["downloadPath"])
        assert fetched.status_code == 200
        assert fetched.content == CSV.encode()
        assert fetched.headers["content-disposition"].startswith("attachment;")
        assert fetched.headers["x-content-type-options"] == "nosniff"


async def test_a_pdf_may_be_rendered_by_the_browser_reader(service):
    app, _, _ = service
    async with client_for(app) as client:
        created = await upload(client, name="report.pdf", body=PDF)
        assert created.status_code == 201, created.text
        file_id = created.json()["data"]["fileId"]

        shown = await client.get(f"/api/files/{file_id}/view")
        assert shown.status_code == 200, shown.text
        assert shown.headers["content-type"] == "application/pdf"
        # Inline is the point: an iframe pointed at an `attachment` downloads the
        # file instead of showing it.
        assert shown.headers["content-disposition"].startswith("inline;")
        assert shown.headers["x-content-type-options"] == "nosniff"
        assert shown.content == PDF


async def test_only_a_pdf_gets_the_inline_door(service):
    app, _, _ = service
    async with client_for(app) as client:
        created = await upload(client)  # a CSV
        file_id = created.json()["data"]["fileId"]
        # Everything else stays an attachment: an uploaded HTML file must never
        # render on this origin, so the view door refuses rather than quietly
        # serving it with a different disposition.
        assert (await client.get(f"/api/files/{file_id}/view")).status_code == 415
        assert (await client.get("/api/files/does-not-exist/view")).status_code == 404


async def test_another_accounts_file_is_not_viewable(service):
    app, store, _ = service
    await store.put("session_files", {
        "id": "foreign-view", "sessionId": SESSION, "name": "theirs.pdf",
        "label": "theirs.pdf", "size": 3, "sha256": "x", "mimeType": "application/pdf",
        "createdAt": "2026-01-01T00:00:00+00:00"}, owner="another-account")
    async with client_for(app) as client:
        # Same 404 as an unknown id: the inline door must not confirm existence.
        assert (await client.get("/api/files/foreign-view/view")).status_code == 404


async def test_an_unknown_type_is_refused_over_http(service):
    app, _, _ = service
    async with client_for(app) as client:
        refused = await upload(client, name="payload.exe", body=b"MZ\x90\x00")
        assert refused.status_code == 415


async def test_the_per_file_limit_is_enforced(tmp_path):
    app, _ = await make_app(tmp_path, upload_max_bytes=1024)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            refused = await upload(client, body=b"a" * 2048)
            assert refused.status_code == 413

            fine = await upload(client, body=b"a" * 512)
            assert fine.status_code == 201


async def test_the_session_file_count_is_capped(tmp_path):
    app, _ = await make_app(tmp_path, upload_max_files=2)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await upload(client)).status_code == 201
            assert (await upload(client, name="b.csv")).status_code == 201
            refused = await upload(client, name="c.csv")
            assert refused.status_code == 409
            assert "Delete one" in refused.json()["detail"]


async def test_the_account_allowance_stops_the_next_upload(tmp_path):
    app, _ = await make_app(tmp_path, upload_max_bytes_per_owner=1024)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await upload(client, body=b"a" * 900)).status_code == 201
            # 900 used of 1024: the remaining 124 is the budget, so 200 bytes fail.
            refused = await upload(client, name="b.csv", body=b"b" * 200)
            assert refused.status_code == 413


async def test_another_accounts_file_is_a_404_not_a_403(service):
    app, store, _ = service
    await store.put("session_files", {
        "id": "foreign-file", "sessionId": SESSION, "name": "theirs.csv",
        "label": "theirs.csv", "size": 3, "sha256": "x", "mimeType": "text/csv",
        "createdAt": "2026-01-01T00:00:00+00:00"}, owner="another-account")
    async with client_for(app) as client:
        assert (await client.get("/api/files/foreign-file/download")).status_code == 404
        assert (await client.delete(f"/api/sessions/{SESSION}/files/foreign-file")).status_code == 404


async def test_another_accounts_conversation_refuses_the_upload(service):
    app, store, _ = service
    await store.put("sessions", {"id": SESSION, "sessionId": SESSION, "isolated": False},
                    owner="another-account")
    async with client_for(app) as client:
        refused = await upload(client)
        assert refused.status_code == 404
        assert (await client.get(f"/api/sessions/{SESSION}/files")).status_code == 404


async def test_a_visitor_cannot_upload(service):
    """A guest owner id is free to mint, so the disk stays behind the account."""
    app, _, _ = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as visitor:
        refused = await upload(visitor)
        assert refused.status_code == 401
        assert refused.json()["detail"]["code"] == "sign_in_required"


async def test_a_file_only_attaches_to_the_conversation_it_belongs_to(tmp_path):
    """An id from another conversation is not a ticket into this one."""
    # The key only gets the endpoint past its readiness probe; the 404 under
    # test happens before a provider is ever contacted.
    app, _ = await make_app(tmp_path, deepseek_api_key="sk-test")
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            created = await upload(client, session="11111111-1111-4111-8111-111111111111")
            file_id = created.json()["data"]["fileId"]
            refused = await client.post("/api/agents/run", json={
                "input": "分析这个文件", "sessionId": SESSION, "turnId": "turn-1",
                "fileIds": [file_id], "stream": False})
            assert refused.status_code == 404


async def test_a_deleted_conversation_takes_its_files_with_it(service):
    app, store, tmp_path = service
    async with client_for(app) as client:
        created = await upload(client)
        assert created.status_code == 201
        # The run would create the row; make it look committed.
        await store.put("sessions", {"id": SESSION, "sessionId": SESSION, "isolated": False})
        assert (await client.delete(f"/api/sessions/{SESSION}")).status_code == 200
        assert (await client.get(created.json()["data"]["downloadPath"])).status_code == 404
    blobs = tmp_path / "workspaces" / "_uploads" / SESSION
    assert not blobs.exists()


async def test_an_oversized_body_is_refused_before_it_is_read(tmp_path):
    """The spool happens before the handler, so the bound lives in middleware."""
    app, _ = await make_app(tmp_path, upload_max_bytes=1024)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            refused = await client.post(
                f"/api/sessions/{SESSION}/files",
                # 64 KB of multipart slack sits above the file limit, so the
                # refusal under test is the middleware one, not the parser's.
                content=b"x" * 70_000,
                headers={"content-type": "multipart/form-data; boundary=----x"})
            assert refused.status_code == 413
            assert "limit" in refused.json()["detail"]
