import asyncio
import json

import httpx
from mongomock_motor import AsyncMongoMockClient
import pytest

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import ToolCall
from agentforge.sandbox.docker_runner import DockerRunner
from agentforge.skill_sources import SkillSource
from agentforge.skills import SkillManager, SkillTool
from agentforge.store import MongoDBStore


@pytest.mark.parametrize("approved", [True, False])
async def test_install_approval_precedes_download_and_catalog_changes(tmp_path, monkeypatch, approved):
    fetched = []

    async def fetch(request, client=None):
        fetched.append(request.url)
        return SkillSource("remote-demo", {"SKILL.md": "Remote instructions"},
                           {"source": "github", "commit": "a" * 40})

    monkeypatch.setattr("agentforge.skill_sources.github_source", fetch)

    class InstallThenList:
        async def chat_stream(self, req):
            assert "install_skill" in [s["function"]["name"] for s in req.tools]
            assert req.messages[-1]["role"] == "system"
            assert "- install_skill:" in req.messages[-1]["content"]
            assert "even when the sandbox is offline" in req.messages[-1]["content"]
            previous = [m for m in req.messages if m["role"] == "tool"]
            if not previous:
                yield ToolCallRequest(call=ToolCall(name="install_skill", arguments={
                    "source": "github", "url": "https://github.com/example/skills/tree/main/demo"}))
            elif len(previous) == 1 and json.loads(previous[0]["content"])["ok"]:
                yield ToolCallRequest(call=ToolCall(name="list_skills", arguments={}))
            else:
                yield ContentDelta(text="Finished")
            yield Done()

    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", InstallThenList())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        await store.put("sessions", {"id": "install", "history": [
            {"role": "user", "content": "Can you install skills?"},
            {"role": "assistant", "content": "No. I only have list_skills and load_skill."}]})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            run = asyncio.create_task(client.post("/api/agents/run", json={
                "input": "Install the specified skill", "sessionId": "install", "stream": True}))
            try:
                for _ in range(300):
                    if app.state.approvals.pending:
                        break
                    await asyncio.sleep(0.01)
                assert app.state.approvals.pending
                assert not fetched and not await store.list("skills")
                identity = next(iter(app.state.approvals.pending))
                response = await client.post(f"/api/approvals/{identity}", json={"approved": approved})
                assert response.status_code == 200
                result = await asyncio.wait_for(run, 10)
            finally:
                if not run.done():
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
            assert bool(fetched) is approved
            assert bool(await store.list("skills")) is approved
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert any(e["type"] == "skill_installed" for e in events) is approved
            assert events[-1]["type"] == "done"
            trace = (await client.get("/api/traces/install")).json()["traces"][0]
            assert trace["tool_calls"][0]["call"]["name"] == "install_skill"
            assert trace["tool_calls"][0]["result"]["ok"] is approved
            if approved:
                assert "remote-demo" in trace["tool_calls"][1]["result"]["output"]


async def test_workspace_skill_install_reads_container_files_without_execution(tmp_path, docker_client):
    runner = DockerRunner(tmp_path, docker_client)
    box = await runner.create()
    manager = SkillManager(MongoDBStore("", "test", client=AsyncMongoMockClient()))
    try:
        prepared = await runner.execute(box, ["python", "-c",
            "from pathlib import Path; p=Path('/workspace/demo/scripts'); p.mkdir(parents=True); "
            "p.parent.joinpath('SKILL.md').write_text('Run scripts/run.py'); "
            "p.joinpath('run.py').write_text(\"from pathlib import Path\\nPath('/workspace/should-not-exist').touch()\")"])
        assert prepared.ok
        tool = SkillTool(manager, "install_skill", runner, box)
        result = await tool.execute({"source": "workspace", "path": "/workspace/demo"})
        assert result.ok and (await manager.catalog())[0]["name"] == "demo"
        assert not (box.workspace.root / "should-not-exist").exists()
        await runner.execute(box, ["python", "-c",
            "from pathlib import Path; p=Path('/workspace/demo/SKILL.md'); p.unlink(); p.symlink_to('/etc/passwd')"])
        blocked = await tool.execute({"source": "workspace", "path": "demo"})
        assert not blocked.ok and "symlinks" in blocked.error
        assert len(await manager.catalog()) == 1
    finally:
        await runner.destroy(box)


async def test_agent_can_install_load_and_run_in_one_task(tmp_path, docker_client):
    class InstallLoadRun:
        async def chat_stream(self, req):
            previous = [m for m in req.messages if m["role"] == "tool"]
            if not previous:
                yield ToolCallRequest(call=ToolCall(name="install_skill", arguments={
                    "source": "inline", "name": "new-skill", "files": {
                        "SKILL.md": "Run scripts/run.py to produce the result.", "scripts/run.py":
                        "from pathlib import Path\nPath('/workspace/installed-run.txt').write_text('42')\nprint(42)"}}))
            elif len(previous) == 1:
                installed = json.loads(json.loads(previous[0]["content"])["output"])
                yield ToolCallRequest(call=ToolCall(name="load_skill", arguments={"skill_id": installed["id"]}))
            elif len(previous) == 2:
                instructions = json.loads(previous[1]["content"])["output"]
                directory = next(line[11:] for line in instructions.splitlines() if line.startswith("Directory: "))
                yield ToolCallRequest(call=ToolCall(name="run_command", arguments={
                    "command": f"python {directory}/scripts/run.py"}))
            else:
                yield ContentDelta(text="Installed and ran skill")
            yield Done()

    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", InstallLoadRun())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            run = asyncio.create_task(client.post("/api/agents/run", json={
                "input": "Create, install and run the specified skill", "sessionId": "install-run",
                "sandbox": True, "stream": True}))
            decisions = set()
            try:
                for _ in range(1000):
                    if run.done():
                        break
                    for identity, future in list(app.state.approvals.pending.items()):
                        if identity not in decisions and not future.done():
                            if not decisions:
                                assert not await store.list("skills")
                            decisions.add(identity)
                            response = await client.post(f"/api/approvals/{identity}", json={"approved": True})
                            assert response.status_code == 200
                    await asyncio.sleep(0.01)
                result = await asyncio.wait_for(run, 5)
            finally:
                if not run.done():
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
            assert len(decisions) == 1
            assert [a["tool"] for a in await store.list("approvals")] == ["install_skill"]
            assert '"type": "skill_installed"' in result.text and '"type": "skill_loaded"' in result.text
            workspace = app.state.sandbox_pool.workspaces["install-run"].root
            assert (workspace / "installed-run.txt").read_text() == "42"
            trace = (await client.get("/api/traces/install-run")).json()["traces"][0]
            assert [c["call"]["name"] for c in trace["tool_calls"]] == ["install_skill", "load_skill", "run_command"]
            assert all(c["result"]["ok"] for c in trace["tool_calls"])
