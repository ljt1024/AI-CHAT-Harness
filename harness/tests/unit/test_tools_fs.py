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
