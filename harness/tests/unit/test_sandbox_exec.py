"""One-shot sandbox execution: run untrusted code without host privileges.

The endpoint exists so host-side tooling can hand a script to the same
offline container the agent uses instead of running it as the user.
"""
import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.models import ToolResult, new_id
from agentforge.sandbox.docker_runner import Sandbox, SandboxUnavailable
from agentforge.sandbox.pool import Lease, SandboxState
from agentforge.store import MongoDBStore
from agentforge.tools.fs import Workspace


class FakeRunner:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else ToolResult(ok=True, output="done")
        self.error = error
        self.calls = []

    async def execute(self, sandbox, command, timeout=30):
        self.calls.append((list(command), timeout))
        if self.error is not None:
            raise self.error
        return self.result


class FakePool:
    def __init__(self, root, runner):
        self.root = root
        self.runner = runner
        self.released = []
        self.acquired = []

    async def acquire(self, session_id, image="python:3.12-slim"):
        self.acquired.append(session_id)
        workspace = Workspace(self.root / session_id)
        sandbox = Sandbox(new_id(), object(), workspace, image)
        return Lease(sandbox, SandboxState.CLAIMED, session_id)

    async def release(self, session_id):
        self.released.append(session_id)


def build(root, runner=None, monkeypatch=None):
    pool = FakePool(root, runner or FakeRunner())
    settings = Settings(agentforge_workspace=root)
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    if monkeypatch is not None:
        monkeypatch.setattr("agentforge.api.SandboxPool", lambda *a, **k: pool)
        application = create_app(settings, store)
    return application, pool


async def test_command_runs_inside_the_sandbox(tmp_path, monkeypatch):
    application, pool = build(tmp_path, monkeypatch=monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as client:
        response = await client.post("/api/sandbox/exec",
                                     json={"command": ["python", "run.py"]})
    assert response.status_code == 200
    assert response.json()["data"]["output"] == "done"
    assert pool.runner.calls[0][0] == ["python", "run.py"]
    # The lease is handed back even on the happy path, or the pool leaks.
    assert pool.released == pool.acquired


async def test_files_are_written_into_the_sandbox_workspace(tmp_path, monkeypatch):
    application, pool = build(tmp_path, monkeypatch=monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as client:
        response = await client.post("/api/sandbox/exec", json={
            "command": ["python", "pkg/run.py"],
            "files": [{"path": "pkg/run.py", "content": "print(1)"}]})
    assert response.status_code == 200
    written = next((tmp_path / session / "pkg" / "run.py" for session in pool.acquired), None)
    assert written is not None and written.read_text() == "print(1)"


async def test_traversal_and_credential_paths_are_refused(tmp_path, monkeypatch):
    application, pool = build(tmp_path, monkeypatch=monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as client:
        escaping = await client.post("/api/sandbox/exec", json={
            "command": ["ls"], "files": [{"path": "../outside.py", "content": "x"}]})
        secret = await client.post("/api/sandbox/exec", json={
            "command": ["ls"], "files": [{"path": ".env", "content": "x"}]})
    assert escaping.status_code == 422
    assert secret.status_code == 422
    assert pool.runner.calls == []


async def test_empty_command_is_rejected(tmp_path, monkeypatch):
    application, _ = build(tmp_path, monkeypatch=monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as client:
        response = await client.post("/api/sandbox/exec", json={"command": []})
    assert response.status_code == 422


async def test_unavailable_docker_reports_503_and_still_releases(tmp_path, monkeypatch):
    runner = FakeRunner(error=SandboxUnavailable("Docker unavailable."))
    application, pool = build(tmp_path, runner=runner, monkeypatch=monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as client:
        response = await client.post("/api/sandbox/exec", json={"command": ["ls"]})
    assert response.status_code == 503
    assert pool.released == pool.acquired
