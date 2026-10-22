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
from agentforge.store import MongoDBStore


class CapabilityAdapter:
    def __init__(self, skill=False):
        self.skill = skill
        self.requests = []

    async def chat_stream(self, req):
        self.requests.append(req)
        if any(m["role"] == "tool" for m in req.messages):
            yield ContentDelta(text="Finished")
        elif self.skill:
            prompt = req.messages[0]["content"]
            directory = next(line[11:] for line in prompt.splitlines() if line.startswith("Directory: "))
            yield ToolCallRequest(call=ToolCall(name="run_command", arguments={
                "command": f"python {directory}/scripts/run.py"}))
        else:
            yield ToolCallRequest(call=ToolCall(name="mcp_local__write_file", arguments={
                "path": "result.txt", "content": "external MCP verified"}))
        yield Done()


async def resolve_run(client, app, approved):
    for _ in range(500):
        if app.state.approvals.pending:
            break
        await asyncio.sleep(0.01)
    assert app.state.approvals.pending, "Expected a real pending approval"
    identity = next(iter(app.state.approvals.pending))
    response = await client.post(f"/api/approvals/{identity}", json={"approved": approved})
    assert response.status_code == 200


@pytest.mark.parametrize("approved", [True, False])
async def test_registered_mcp_is_used_by_runtime(tmp_path, mcp_service, approved):
    url, workspace = mcp_service
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    adapter = CapabilityAdapter()
    gateway.register("deepseek", adapter)
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            registered = await client.post("/api/mcp/servers", json={"id": "local", "url": url})
            assert registered.status_code == 201
            tools = registered.json()["data"]["tools"]
            assert "mcp_local__write_file" in [tool["name"] for tool in tools]
            assert (await client.get("/api/mcp/servers")).json()["data"][0]["id"] == "local"
            run = asyncio.create_task(client.post("/api/agents/run", json={
                "input": "Write via MCP", "sessionId": "mcp-test", "stream": True,
                "mcp_servers": ["local"]}))
            try:
                for _ in range(500):
                    if app.state.approvals.pending:
                        break
                    await asyncio.sleep(0.01)
                assert not (workspace / "result.txt").exists()
                await resolve_run(client, app, approved)
                result = await asyncio.wait_for(run, 10)
            finally:
                if not run.done():
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["type"] == "done"
            assert (workspace / "result.txt").exists() is approved
            assert not (tmp_path / "mcp-test" / "result.txt").exists()
            assert "mcp_local__write_file" in [s["function"]["name"] for s in adapter.requests[0].tools]
            trace = (await client.get("/api/traces/mcp-test")).json()["traces"][0]
            assert trace["tool_calls"][0]["result"]["ok"] is approved
            created = await client.post("/api/agents", json={"name": "MCP agent", "mcp_servers": ["local"]})
            assert created.status_code == 201
            assert (await client.delete("/api/mcp/servers/local")).status_code == 409


@pytest.mark.parametrize("approved", [True, False])
async def test_installed_skill_respects_explicit_command_approval(tmp_path, docker_client, approved):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    adapter = CapabilityAdapter(skill=True)
    gateway.register("deepseek", adapter)
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            installed = await client.post("/api/skills", json={"name": "demo", "files": {
                "SKILL.md": "Run scripts/run.py to produce the report.",
                "scripts/run.py": "from pathlib import Path\nPath('/workspace/report.txt').write_text('42')\nprint(42)"}})
            assert installed.status_code == 201
            identity = installed.json()["data"]["id"]
            assert (await client.get("/api/skills")).json()["data"][0]["files"]
            denied = await client.post("/api/agents/run", json={"input": "Run", "sessionId": "no-sandbox",
                                                                   "skills": [identity]})
            assert denied.status_code == 422
            created = await client.post("/api/agents", json={"name": "Strict skill execution",
                "tools": ["run_command"], "skills": [identity], "sandbox_policy": {},
                "permissions": {"run_command": "confirm"}})
            assert created.status_code == 201
            run = asyncio.create_task(client.post("/api/agents/run", json={
                "input": "Run skill", "sessionId": "skill-test", "stream": True,
                "agentIds": [created.json()["data"]["id"]]}))
            try:
                await resolve_run(client, app, approved)
                result = await asyncio.wait_for(run, 15)
            finally:
                if not run.done():
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
            assert '"type": "done"' in result.text
            workspace = app.state.sandbox_pool.workspaces["skill-test"].root
            assert (workspace / "report.txt").exists() is approved
            assert not (tmp_path / "skill-test" / "report.txt").exists()
            assert "Run scripts/run.py" in adapter.requests[0].messages[0]["content"]
            trace = (await client.get("/api/traces/skill-test")).json()["traces"][0]
            assert trace["turns"][0]["middleware"]["skills"]["changed_files"] == 2
            assert trace["turns"][1]["middleware"]["skills"]["changed_files"] == 0
            assert (await client.delete(f"/api/skills/{identity}")).status_code == 409
            assert (await client.delete("/api/skills/missing-0123456789ab")).status_code == 404
            await store.db.agents.delete_one({"id": created.json()["data"]["id"]})
            uninstalled = await client.delete(f"/api/skills/{identity}")
            assert uninstalled.status_code == 200
            assert uninstalled.json()["data"]["removed_workspaces"] >= 1
            assert not (workspace / ".skills" / identity).exists()
            assert (await client.get("/api/skills")).json()["data"] == []


async def test_invalid_capabilities_fail_before_start(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", CapabilityAdapter())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for capability in ({"mcp_servers": ["missing"]}, {"sandbox": True, "skills": ["missing"]}):
                response = await client.post("/api/agents/run", json={
                    "input": "Run", "sessionId": "invalid", **capability})
                assert response.status_code == 422
            response = await client.post("/api/mcp/servers", json={"id": "local", "url": "file:///etc/passwd"})
            assert response.status_code == 422
            response = await client.post("/api/mcp/servers", json={"id": "offline", "url": "http://127.0.0.1:1/mcp"})
            assert response.status_code == 502


async def test_agent_discovers_loads_and_executes_skill_without_preselection(tmp_path, docker_client):
    class AutonomousSkills:
        async def chat_stream(self, req):
            previous = [m for m in req.messages if m["role"] == "tool"]
            if not previous:
                yield ToolCallRequest(call=ToolCall(name="list_skills", arguments={}))
            elif len(previous) == 1:
                catalog = json.loads(json.loads(previous[-1]["content"])["output"])
                yield ToolCallRequest(call=ToolCall(name="load_skill", arguments={
                    "skill_id": catalog["skills"][0]["id"]}))
            elif len(previous) == 2:
                instructions = json.loads(previous[-1]["content"])["output"]
                directory = next(line[11:] for line in instructions.splitlines() if line.startswith("Directory: "))
                yield ToolCallRequest(call=ToolCall(name="run_command", arguments={
                    "command": f"python {directory}/scripts/run.py"}))
            else:
                yield ContentDelta(text="Skill executed successfully")
            yield Done()

    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    gateway = ModelGateway()
    gateway.register("deepseek", AutonomousSkills())
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), store, gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            installed = await client.post("/api/skills", json={"name": "autonomous", "files": {
                "SKILL.md": "Run scripts/run.py to create the result.", "scripts/run.py":
                "from pathlib import Path\nPath('/workspace/autonomous.txt').write_text('42')\nprint(42)"}})
            identity = installed.json()["data"]["id"]
            run = asyncio.create_task(client.post("/api/agents/run", json={
                "input": "Find a skill and use it", "sessionId": "auto-skills", "sandbox": True,
                "stream": True}))
            try:
                result = await asyncio.wait_for(run, 15)
            finally:
                if not run.done():
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
            assert '"type": "done"' in result.text
            trace = (await client.get("/api/traces/auto-skills")).json()["traces"][0]
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert not any(e["type"] == "await_approval" for e in events)
            loaded = next(e for e in events if e["type"] == "skill_loaded")
            assert loaded["skill_id"] == identity and loaded["sessionId"] == "auto-skills"
            assert [c["call"]["name"] for c in trace["tool_calls"]] == ["list_skills", "load_skill", "run_command"]
            assert all(c["result"]["ok"] for c in trace["tool_calls"])
            session = await store.get("sessions", "auto-skills")
            assert session["loaded_skills"] == [identity]
            workspace = app.state.sandbox_pool.workspaces["auto-skills"].root
            assert (workspace / "autonomous.txt").read_text() == "42"
            assert not (tmp_path / "auto-skills" / "autonomous.txt").exists()
            again = await client.post("/api/agents/run", json={"input": "Continue", "sessionId": "auto-skills",
                                                                         "sandbox": True, "stream": True})
            assert '"type": "done"' in again.text
            traces = (await client.get("/api/traces/auto-skills")).json()["traces"]
            latest = next(t for t in traces if t["id"] != trace["id"])
            assert latest["turns"][0]["middleware"]["skills"]["changed_files"] == 0
