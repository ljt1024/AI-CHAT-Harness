import io
import tarfile
from types import SimpleNamespace

import httpx
from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError
import pytest

from agentforge.skill_sources import (
    SkillInstallRequest, github_location, github_source, read_workspace_archive,
)
from agentforge.skills import SkillManager, SkillPackage, SkillTool
from agentforge.store import MongoDBStore


@pytest.mark.parametrize("url", ["http://github.com/o/r", "https://github.com.evil.test/o/r",
    "https://api.github.com/repos/o/r", "https://u:p@github.com/o/r", "https://github.com/o/r?token=x",
    "file:///etc/passwd", "http://127.0.0.1/private", "https://github.com/o/r/tree/main/../secret"])
def test_install_rejects_untrusted_urls(url):
    with pytest.raises(ValidationError):
        SkillInstallRequest(source="github", url=url)


@pytest.mark.parametrize("args", [{"source": "inline", "name": "demo"},
    {"source": "workspace", "path": "/etc/private"},
    {"source": "workspace", "path": "../private"},
    {"source": "inline", "name": "demo", "files": {"SKILL.md": "x"}, "url": "https://github.com/o/r"}])
def test_install_requires_unambiguous_source(args):
    with pytest.raises(ValidationError):
        SkillInstallRequest(**args)


def test_github_tree_blob_and_slash_revision_parsing():
    for request in [SkillInstallRequest(source="github", url="https://github.com/o/r/tree/main/skills/demo"),
        SkillInstallRequest(source="github", url="https://github.com/o/r/blob/main/skills/demo/SKILL.md"),
        SkillInstallRequest(source="github", url="https://github.com/o/r", revision="main", path="skills/demo")]:
        assert github_location(request) == ("o", "r", "main", "skills/demo")
    request = SkillInstallRequest(source="github", url="https://github.com/o/r/tree/feature/skills/skills/demo",
                                  revision="feature/skills")
    assert github_location(request) == ("o", "r", "feature/skills", "skills/demo")


async def test_github_download_is_commit_pinned_and_does_not_execute():
    calls = []
    commit = "a" * 40
    files = {"skills/demo/SKILL.md": b"# Demo\nRun scripts/run.py",
             "skills/demo/scripts/run.py": b"raise RuntimeError('must not execute')"}

    def handler(request):
        calls.append(request)
        path = request.url.path
        if request.url.host == "api.github.com":
            if path.endswith("/commits/main"):
                return httpx.Response(200, json={"sha": commit})
            assert request.url.params["recursive"] == "1"
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": "skills/demo", "type": "tree", "mode": "040000"},
                {"path": "skills/demo/scripts", "type": "tree", "mode": "040000"}]
                + [{"path": name, "type": "blob", "mode": "100644", "size": len(content)}
                   for name, content in files.items()]})
        assert request.url.host == "raw.githubusercontent.com"
        assert path.split("/")[3] == commit  # raw downloads are pinned to the resolved commit
        return httpx.Response(200, content=files[path.split("/", 4)[-1]])

    request = SkillInstallRequest(source="github", url="https://github.com/o/r/tree/main/skills/demo")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = await github_source(request, client)
    assert source.name == "demo"
    assert source.provenance["commit"] == commit
    assert source.files["scripts/run.py"].startswith("raise RuntimeError")
    assert len(calls) == 4  # commit, recursive tree, then one raw download per file
    assert SkillPackage(name=source.name, files=source.files)


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
async def test_github_failures_do_not_follow_redirects_or_leak_responses(status):
    def handler(request):
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/private"},
                              text="private provider diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError) as exc:
            await github_source(SkillInstallRequest(source="github", url="https://github.com/o/r"), client)
    assert "private" not in str(exc.value)


async def test_github_rejects_unbounded_metadata():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"x" * 2_000_001))) as client:
        with pytest.raises(ValueError, match="download limit"):
            await github_source(SkillInstallRequest(source="github", url="https://github.com/o/r"), client)


async def test_github_accepts_megabyte_skill_data_files():
    """Real skills (ui-ux-pro-max) ship data files of hundreds of KB."""
    commit = "a" * 40
    payload = ",".join(["x" * 99 for _ in range(8400)])  # ~832 KB
    files = {"skills/demo/SKILL.md": b"# Demo", "skills/demo/data/big.csv": payload.encode()}

    def handler(request):
        path = request.url.path
        if path.endswith("/commits/HEAD"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": name, "type": "blob", "mode": "100644", "size": len(content)}
                for name, content in files.items()]})
        return httpx.Response(200, content=files[path.split("/", 4)[-1]])

    request = SkillInstallRequest(source="github", url="https://github.com/o/r/tree/HEAD/skills/demo")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = await github_source(request, client)
    assert len(source.files["data/big.csv"]) == len(payload)
    assert SkillPackage(name=source.name, files=source.files)


def commit_tarball(files, root="r-main"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(f"{root}/{name}")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return data.getvalue()


async def test_github_falls_back_to_commit_archive_when_raw_is_unreachable():
    """Some networks cannot reach raw.githubusercontent.com; the archive is one request."""
    commit = "a" * 40
    files = {"skills/demo/SKILL.md": b"# Demo", "skills/demo/scripts/run.py": b"print(42)"}
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "raw.githubusercontent.com":
            raise httpx.ConnectError("raw unreachable")
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=commit_tarball(files))
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(200, json={"truncated": False, "tree": [
            {"path": name, "type": "blob", "mode": "100644", "size": len(content)}
            for name, content in files.items()]})

    request = SkillInstallRequest(source="github", url="https://github.com/o/r/tree/main/skills/demo")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = await github_source(request, client)
    assert source.files["scripts/run.py"] == "print(42)"
    assert hosts.count("api.github.com") == 2  # commit + tree only, no per-file blob calls


async def test_github_uses_few_api_calls_for_many_files():
    """Raw downloads keep multi-file skills far below the unauthenticated API limit."""
    commit = "a" * 40
    api_calls = 0

    def handler(request):
        nonlocal api_calls
        if request.url.host == "api.github.com":
            api_calls += 1
            if request.url.path.endswith("/commits/main"):
                return httpx.Response(200, json={"sha": commit})
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": f"skills/demo/f{i}.csv", "type": "blob", "mode": "100644", "size": 3}
                for i in range(70)] + [
                {"path": "skills/demo/SKILL.md", "type": "blob", "mode": "100644", "size": 4}]})
        return httpx.Response(200, content=b"csv" if request.url.path.endswith(".csv") else b"# hi")

    request = SkillInstallRequest(source="github", url="https://github.com/o/r/tree/main/skills/demo")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = await github_source(request, client)
    assert len(source.files) == 71
    assert api_calls == 2


def archive_container(members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, kind, content in members:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind == tarfile.REGTYPE:
                entry.size = len(content)
            else:
                entry.linkname = "/etc/passwd"
            archive.addfile(entry, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return SimpleNamespace(get_archive=lambda path: (iter([data.getvalue()]), {"name": "demo"}))


@pytest.mark.parametrize("name,kind", [("demo/../escape", tarfile.REGTYPE),
    ("/etc/private", tarfile.REGTYPE), ("demo/SKILL.md", tarfile.SYMTYPE),
    ("demo/SKILL.md", tarfile.LNKTYPE), ("other/SKILL.md", tarfile.REGTYPE)])
def test_workspace_archive_rejects_unsafe_entries(name, kind):
    with pytest.raises(ValueError):
        read_workspace_archive(archive_container([(name, kind, b"unsafe")]), "demo")


def test_workspace_archive_preserves_large_text_without_tool_output_truncation():
    container = archive_container([("demo/SKILL.md", tarfile.REGTYPE, b"Instructions"),
        ("demo/scripts/run.py", tarfile.REGTYPE, b"# source\n" * 1000)])
    files = read_workspace_archive(container, "demo")
    assert len(files["scripts/run.py"]) == 9000


async def test_inline_install_is_confirm_required_idempotent_and_discoverable():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    tool = SkillTool(manager, "install_skill")
    assert tool.permission == "confirm"
    args = {"source": "inline", "name": "demo", "files": {"SKILL.md": "Test instructions"}}
    first, second = await tool.execute(args), await tool.execute(args)
    assert first.ok and second.ok
    catalog = await manager.catalog()
    assert len(catalog) == 1
    assert catalog[0]["id"] in first.output


async def test_invalid_package_is_not_saved_and_workspace_needs_sandbox():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    tool = SkillTool(SkillManager(store), "install_skill")
    invalid = await tool.execute({"source": "inline", "name": "demo", "files": {"readme.txt": "No skill"}})
    assert not invalid.ok and "SKILL.md" in invalid.error
    blocked = await tool.execute({"source": "workspace", "path": "demo"})
    assert not blocked.ok and "sandbox" in blocked.error
    assert not await store.list("skills")
