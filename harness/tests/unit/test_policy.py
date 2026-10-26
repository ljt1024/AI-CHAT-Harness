"""The intervention policy: where a run stops, and what it stops for."""
import asyncio

import httpx
from mongomock_motor import AsyncMongoMockClient

from agentforge.api import create_app
from agentforge.config import Settings
from agentforge.events import EventBus
from agentforge.gateway.base import ContentDelta, Done, ToolCallRequest
from agentforge.gateway.registry import ModelGateway
from agentforge.models import AgentDefinition, ToolCall
from agentforge.permissions import ApprovalManager, current_answer
from agentforge.policy import InterventionPolicy
from agentforge.store import MongoDBStore
from agentforge.tools.ask import AskUserTool
from agentforge.tools.fs import FileTool, Workspace


def make_store():
    return MongoDBStore("", "test", client=AsyncMongoMockClient())


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def policy(**overrides):
    config = {
        "defaults": {"level": "auto"},
        "tools": {
            "run_command": {
                "level": "rule", "in_sandbox": "auto",
                "rules": [
                    {"level": "confirm", "command_matches": ["rm\\s+-rf"]},
                    {"level": "auto", "command_matches": ["^(ls|pwd)\\b"]},
                    {"level": "confirm", "reason": "not routine"},
                ],
            },
            "write_file": {
                "level": "rule",
                "rules": [
                    {"level": "deny", "path_matches": ["**/.env", "*secret*"]},
                    {"level": "confirm", "path_outside_workspace": True},
                    {"level": "auto"},
                ],
            },
            "ask_user": {"level": "confirm", "kind": "input"},
            "mcp:*": "confirm",
            "mcp:read_*": "auto",
        },
        "model_requests": {"enabled": True, "max_per_run": 2, "timeout": 0.05,
                           "forbid_patterns": ["api[_-]?key"]},
    }
    config.update(overrides)
    return InterventionPolicy(config)


def manager(**overrides):
    return ApprovalManager(make_store(), 0.05, policy(**overrides))


def silent():
    async def emit(*args, **kwargs):
        pass
    return emit


def asking(question):
    return ToolCall(name="ask_user", arguments={"question": question})


# ---------- the policy file itself ----------

def test_shipped_policy_describes_its_tools():
    loaded = InterventionPolicy.load("../config/intervention.yaml")
    assert loaded.tools, "the shipped policy must describe at least one tool"
    assert loaded.decide("ask_user", {}).kind == "input"


def test_missing_or_broken_policy_degrades_to_tool_defaults(tmp_path):
    assert InterventionPolicy.load(tmp_path / "nope.yaml").tools == {}
    broken = tmp_path / "broken.yaml"
    broken.write_text("tools: [this is not a mapping\n", encoding="utf-8")
    assert InterventionPolicy.load(broken).tools == {}


def test_unknown_tool_falls_back_to_the_tool_itself():
    assert policy().decide("something_new", {}, default_level="confirm").level == "confirm"


# ---------- levels ----------

def test_a_rule_may_tighten_a_tool_but_never_loosen_it():
    assert policy().decide("run_command", {"command": "rm -rf /"},
                           default_level="confirm").level == "confirm"
    # "pwd" is routine, yet the host shell is deliberately stricter than that.
    assert policy().decide("run_command", {"command": "pwd"},
                           default_level="confirm").level == "confirm"


def test_sandbox_fallback_keeps_routine_work_quiet():
    assert policy().decide("run_command", {"command": "python app.py"},
                           default_level="rule", in_sandbox=True).level == "auto"
    assert policy().decide("run_command", {"command": "python app.py"},
                           default_level="confirm").level == "confirm"


def test_credential_paths_are_denied_outright():
    assert policy().decide("write_file", {"path": ".env"}, default_level="rule").level == "deny"
    assert policy().decide("write_file", {"path": "config/secrets/key.txt"},
                           default_level="rule").level == "deny"
    # Ordinary writes keep the tool's own "rule" level, which still checks the path.
    assert policy().decide("write_file", {"path": "notes.md"}, default_level="rule").level == "rule"


def test_escape_from_the_workspace_is_confirmed(tmp_path):
    assert policy().decide("write_file", {"path": "../outside.txt"}, default_level="rule",
                           workspace_root=tmp_path).level == "confirm"


def test_an_explicit_entry_may_relax_what_a_tool_defaults_to():
    assert policy().decide("mcp:read_thing", {}, default_level="confirm").level == "auto"
    assert policy().decide("mcp:write_thing", {}, default_level="confirm").level == "confirm"


# ---------- asking for information ----------

async def test_question_is_answered_and_the_run_continues():
    approvals = manager()
    bus = EventBus()
    async def emit(kind, **data):
        await bus.emit({"type": kind, **data})

    async def run_and_read():
        # A task gets a copy of the context, so the answer has to be read on the
        # same task that ran the gate -- exactly how a tool sees it in a real run.
        permitted = await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                          asking("Which region?"), emit, session_id="s1")
        return permitted, current_answer.get()

    task = asyncio.create_task(run_and_read())
    async for event in bus.stream():
        if event["type"] == "await_input":
            approvals.resolve(event["call_id"], True, "ap-southeast")
            break
    permitted, answer = await task
    assert permitted is True
    assert answer == "ap-southeast"


async def test_question_with_no_answer_still_continues():
    approvals = manager()
    assert await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                 asking("Anything to add?"), silent(), session_id="s1") is True
    assert not current_answer.get(), "a skipped question must not be reported as an answer"


async def test_question_budget_prevents_a_model_from_interrogating():
    approvals = manager()
    for _ in range(2):
        assert await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                     asking("q"), silent(), session_id="s1") is True
    assert not approvals.pending, "the budget is 2, so the third question never suspends"
    assert await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                 asking("q"), silent(), session_id="s1") is True


async def test_credential_fishing_is_refused_before_anyone_is_asked():
    approvals = manager()
    asked = []
    async def emit(kind, **data):
        asked.append(kind)
    assert await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                 asking("What is your api_key?"), emit, session_id="s1") is True
    assert not asked, "a question about credentials must never reach the human"
    assert "not allowed" in (current_answer.get() or "")


async def test_asking_can_be_switched_off_entirely():
    approvals = manager(model_requests={"enabled": False})
    asked = []
    async def emit(kind, **data):
        asked.append(kind)
    assert await approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                 asking("q"), emit, session_id="s1") is True
    assert not asked


async def test_pending_requests_say_whether_they_are_a_gate_or_a_question():
    approvals = manager()
    task = asyncio.create_task(approvals.check(AgentDefinition(name="t"), AskUserTool(),
                                               asking("q"), silent(), session_id="s1"))
    await asyncio.sleep(0.01)
    pending = approvals.pending_list()
    assert [item["kind"] for item in pending] == ["input"]
    task.cancel()


async def test_policy_denial_outranks_an_agent_that_wants_to_approve(tmp_path):
    approvals = manager()
    agent = AgentDefinition(name="t", permissions={"write_file": "confirm"})
    assert not await approvals.check(agent, FileTool(Workspace(tmp_path), "write_file"),
                                     ToolCall(name="write_file", arguments={"path": ".env"}),
                                     silent())


# ---------- the endpoint that explains all of it ----------

async def test_ask_user_reaches_the_model_end_to_end(tmp_path):
    """The full path: model asks, human answers through the API, model receives it."""
    class AskThenAnswer:
        def __init__(self):
            self.answers = []

        async def chat_stream(self, req):
            if any(m["role"] == "tool" for m in req.messages):
                self.answers.append([m["content"] for m in req.messages if m["role"] == "tool"][-1])
                yield ContentDelta(text="done")
            else:
                yield ToolCallRequest(call=ToolCall(name="ask_user",
                                                    arguments={"question": "Which region?"}))
            yield Done()

    adapter = AskThenAnswer()
    gateway = ModelGateway()
    gateway.register("deepseek", adapter)
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), make_store(), gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            created = await client.post("/api/agents", json={"name": "Asker", "tools": ["ask_user"]})
            agent_id = created.json()["data"]["id"]
            started = await client.post("/api/sessions", json={"agent_id": agent_id, "task": "Pick a region"})
            session_id = started.json()["session_id"]
            assert await wait_until(lambda: bool(app.state.approvals.pending)), "no question asked"
            pending = app.state.approvals.pending_list()[0]
            assert pending["kind"] == "input" and pending["sessionId"] == session_id
            response = await client.post(f"/api/approvals/{pending['id']}",
                                         json={"approved": True, "reason": "ap-southeast"})
            assert response.status_code == 200
            assert await wait_until(lambda: bool(adapter.answers)), "answer never reached the model"
    assert "ap-southeast" in adapter.answers[0]


async def test_policy_endpoint_explains_every_intervention(tmp_path):
    app = create_app(Settings(_env_file=None, agentforge_workspace=tmp_path,
                              sandbox_warm_size=0), make_store(), ModelGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api/interventions/policy")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "run_command" in data["tools"] and "ask_user" in data["tools"]
    assert data["model_requests"]["max_per_run"] >= 1
