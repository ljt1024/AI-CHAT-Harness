import pytest

from agentforge.tools.fs import Workspace
from agentforge.tools.shell import ShellTool


@pytest.mark.parametrize("command", ["rm -rf .", "cat ../secret", "cat .env", "env", "sh -c pwd",
                                     "echo $(env)", "pwd; env", "python -c print(1)"])
async def test_denials(tmp_path, command):
    result = await ShellTool(Workspace(tmp_path)).execute({"command": command})
    assert not result.ok


async def test_success_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_API_KEY", "must-not-appear")
    tool = ShellTool(Workspace(tmp_path), timeout=0.05)
    assert (await tool.execute({"command": "echo hello"})).output.strip() == "hello"
    assert "timed out" in (await tool.execute({"command": "sleep 1"})).error
    assert not (await tool.execute({"command": "false"})).ok
