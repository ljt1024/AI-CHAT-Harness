import pytest

from agentforge.tools.base import ToolRegistry
from agentforge.tools.fs import FileTool, Workspace


@pytest.fixture
def registry(tmp_path):
    registry = ToolRegistry()
    for name in ("read_file", "write_file", "grep", "glob"):
        registry.register(FileTool(Workspace(tmp_path), name))
    return registry


async def test_files(registry):
    assert (await registry.execute("write_file", {"path": "a.txt", "content": "needle\nother"})).ok
    assert (await registry.execute("read_file", {"path": "a.txt"})).output == "needle\nother"
    assert "a.txt:1:needle" in (await registry.execute("grep", {"query": "needle"})).output
    assert (await registry.execute("glob", {"pattern": "*.txt"})).output == "a.txt"


async def test_file_chunks_append_only_when_requested(registry, tmp_path):
    assert (await registry.execute("write_file", {"path": "a.txt", "content": "first"})).ok
    assert (await registry.execute("write_file", {"path": "a.txt", "content": "second", "append": True})).ok
    assert (tmp_path / "a.txt").read_text() == "firstsecond"
    assert (await registry.execute("write_file", {"path": "a.txt", "content": "replacement"})).ok
    assert (tmp_path / "a.txt").read_text() == "replacement"
    assert not (await registry.execute("write_file", {"path": "a.txt", "content": "bad", "append": "true"})).ok
    assert not (await registry.execute("write_file", {"path": "../escape", "content": "bad", "append": True})).ok


async def test_read_pagination_has_exact_unicode_character_offsets(registry, tmp_path):
    content = "技能说明" * 700
    (tmp_path / "skill.txt").write_text(content, encoding="utf-8")
    first = await registry.execute("read_file", {"path": "skill.txt"})
    assert first.ok and first.output.startswith(content[:1200])
    assert "next_offset=1200" in first.output
    second = await registry.execute("read_file", {"path": "skill.txt", "offset": 1200})
    assert second.output.startswith(content[1200:2400])
    assert "next_offset=2400" in second.output
    last = await registry.execute("read_file", {"path": "skill.txt", "offset": 2400})
    assert last.output == content[2400:] + "\n[EOF. Read characters 2400:2800.]"


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", ".env", "nested/.env.local", "key.pem"])
async def test_escape_and_credentials(registry, path):
    assert not (await registry.execute("read_file", {"path": path})).ok
    assert not (await registry.execute("write_file", {"path": path, "content": "bad"})).ok


async def test_symlink_and_schema(registry, tmp_path):
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    assert not (await registry.execute("write_file", {"path": "escape/out", "content": "bad"})).ok
    assert not (await registry.execute("write_file", {"path": "a"})).ok
    (tmp_path / "big.txt").write_text("x" * 5000)
    result = await registry.execute("read_file", {"path": "big.txt"})
    assert len(result.output) <= 4000
    assert "Truncated" in result.output
