import asyncio
from contextvars import ContextVar
import logging

from agentforge.models import new_id
from agentforge.policy import LEVELS, Decision, InterventionPolicy
from agentforge.sandbox.tools import SandboxTool

logger = logging.getLogger(__name__)

CANCEL_REASON = "Approval cancelled: the run was stopped."

# Carries a human's answer from the permission gate back to the tool that asked for
# it. The gate and the tool run in the same task, so a context var reaches it.
current_answer: ContextVar[str | None] = ContextVar("agentforge_answer", default=None)


class ApprovalManager:
    def __init__(self, store, timeout=300, policy: InterventionPolicy | None = None):
        self.store, self.timeout = store, timeout
        self.policy = policy or InterventionPolicy()
        self.pending: dict[str, asyncio.Future] = {}
        # Approval id -> session that requested it. Lets a stopped run release
        # whatever is waiting on it instead of hanging until the timeout.
        self.owners: dict[str, str] = {}
        # Questions asked per session, so a model cannot interrogate forever.
        self.asked: dict[str, int] = {}
        # Request id -> "approve" or "input", so a reconnecting client can tell a
        # gate from a question.
        self.kinds: dict[str, str] = {}
        # Request id -> tool name, so "allow this tool for the rest of the session"
        # knows what it is granting without trusting the client.
        self.tools: dict[str, str] = {}
        # Request id -> may this grant be remembered at all? The server decides, so
        # a client cannot ask for a session-wide pass on a host shell command.
        self.grants: dict[str, bool] = {}
        # Session -> tools a human already approved for every later call. Held in
        # memory on purpose: a restart asks again rather than silently inheriting
        # a blanket permission.
        self.remembered: dict[str, set[str]] = {}

    def _workspace_root(self, tool):
        workspace = getattr(tool, "workspace", None)
        return getattr(workspace, "root", None)

    def _resolve_level(self, agent, tool, arguments):
        """Strictest of what the policy says and what the agent configuration says."""
        # "in_sandbox" means the container is actually running: a dead container must
        # not inherit the relaxed fallback, it fails closed through the normal rules.
        sandbox = getattr(tool, "sandbox", None)
        decision = self.policy.decide(tool.name, arguments, default_level=tool.permission,
                                      in_sandbox=isinstance(tool, SandboxTool)
                                      and bool(getattr(sandbox, "alive", False)),
                                      workspace_root=self._workspace_root(tool))
        if decision.level == "deny":
            # A policy denial outranks any agent configuration; otherwise an agent
            # could escalate a forbidden action into merely "needs approval".
            return decision
        agent_level = str(agent.permissions.get(tool.name, "auto"))
        if LEVELS.get(agent_level, 0) > LEVELS.get(decision.level, 0):
            return Decision(level=agent_level, kind=decision.kind, reason=decision.reason)
        return decision

    async def check(self, agent, tool, call, emit, session_id=""):
        decision = self._resolve_level(agent, tool, call.arguments)
        level = decision.level
        if level == "auto":
            return True
        # Every gate is worth one line: it explains why a run paused, and a
        # silent "auto" is the difference between a stuck run and a fast one.
        logger.info("permission session=%s tool=%s level=%s kind=%s reason=%s", session_id,
                    tool.name, level, decision.kind, decision.reason)
        if level == "rule":
            # Trust the real Docker isolation boundary, not command-text heuristics.
            # Host shell, installers and remote MCP tools never enter this rule.
            if tool.name == "run_command" and isinstance(tool, SandboxTool):
                return tool.sandbox.alive
            if tool.name == "write_file" and tool.source == "builtin":
                try:
                    path = tool.workspace.path(call.arguments["path"])
                    if any(part.startswith(".") for part in path.relative_to(tool.workspace.root).parts):
                        return False
                    return True
                except (ValueError, KeyError):
                    return False
            # A "rule" the policy does not know how to judge still stops and asks.
            return await self._confirm(tool, call, emit, session_id,
                                       Decision(level="confirm", reason="Not on the routine allowlist."))
        if level == "deny":
            await emit("approval_resolved", call_id=call.id, approved=False,
                       reason=decision.reason or "Denied by policy.")
            return False
        if decision.kind == "input":
            return await self._ask(agent, tool, call, emit, session_id, decision)
        if session_id and tool.name in self.remembered.get(session_id, set()):
            # The human already said "yes, this tool, this conversation". Denials
            # and policy stops never reach here, so a grant cannot widen them.
            logger.info("approval skipped session=%s tool=%s reason=remembered", session_id,
                        tool.name)
            return True
        return await self._confirm(tool, call, emit, session_id, decision)

    async def _ask(self, agent, tool, call, emit, session_id, decision):
        """The model is missing information. Fails open: no answer is a worse run, not an unsafe one."""
        current_answer.set(None)
        if not self.policy.input_enabled:
            return True
        question = str(call.arguments.get("question", "") or call.arguments.get("prompt", ""))
        allowed, refusal = self.policy.input_allowed(question)
        if not allowed:
            current_answer.set(refusal)
            return True
        if self.asked.get(session_id, 0) >= self.policy.input_budget:
            current_answer.set("The human is not available for further questions; "
                               "continue with your best judgement.")
            return True
        identity = new_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        self.kinds[identity] = "input"
        if session_id:
            self.owners[identity] = session_id
            self.asked[session_id] = self.asked.get(session_id, 0) + 1
        record = {"id": identity, "tool_call_id": call.id, "tool": call.name,
                  "arguments": call.arguments, "status": "pending", "kind": "input"}
        if session_id:
            record["sessionId"] = session_id
        try:
            await self.store.put("approvals", record)
            await emit("await_input", call_id=identity, tool_call_id=call.id, tool=call.name,
                       question=question, arguments=call.arguments)
            timeout = decision.timeout if decision.timeout is not None else self.policy.input_timeout
            try:
                answered, answer = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                answered, answer = False, ""
            current_answer.set(answer if answered and answer else "")
            logger.info("input session=%s tool=%s answered=%s", session_id, tool.name, answered)
            await self.store.put("approvals", {**record,
                "status": "answered" if answered else "skipped", "reason": answer})
            await emit("approval_resolved", call_id=identity, approved=answered, reason=answer)
            # Always continue: a missing answer must not look like a refusal.
            return True
        finally:
            self.pending.pop(identity, None)
            self.owners.pop(identity, None)
            self.kinds.pop(identity, None)

    async def _confirm(self, tool, call, emit, session_id, decision):
        identity = new_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        self.kinds[identity] = "approve"
        self.tools[identity] = tool.name
        if session_id:
            self.owners[identity] = session_id
        record = {"id": identity, "tool_call_id": call.id, "tool": call.name,
                  "arguments": call.arguments, "status": "pending", "kind": "approve"}
        # Only a remote service can be let off for the whole session: its calls are
        # one bounded surface the human just looked at. A shell command or a file
        # write can do something entirely different next time, so it keeps asking.
        rememberable = getattr(tool, "source", "") == "mcp" and bool(session_id)
        self.grants[identity] = rememberable
        if session_id:
            record["sessionId"] = session_id
        try:
            await self.store.put("approvals", record)
            await emit("await_approval", call_id=identity, tool_call_id=call.id,
                       tool=call.name, arguments=call.arguments, reason=decision.reason,
                       rememberable=rememberable)
            timeout = decision.timeout if decision.timeout is not None else self.timeout
            try:
                approved, reason = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                approved, reason = False, "Approval timed out; denied by default."
            logger.info("approval session=%s tool=%s approved=%s reason=%s", session_id,
                        tool.name, approved, reason)
            await self.store.put("approvals", {**record,
                "status": "approved" if approved else "denied", "reason": reason})
            await emit("approval_resolved", call_id=identity, approved=approved, reason=reason)
            return approved
        finally:
            self.pending.pop(identity, None)
            self.owners.pop(identity, None)
            self.kinds.pop(identity, None)
            self.tools.pop(identity, None)
            self.grants.pop(identity, None)

    def resolve(self, call_id: str, approved: bool, reason: str = "", remember: bool = False):
        future = self.pending.get(call_id)
        if future is None or future.done():
            raise ValueError("Approval is expired or already resolved.")
        if remember and approved is True and self.grants.get(call_id):
            session_id = self.owners.get(call_id)
            tool_name = self.tools.get(call_id)
            if session_id and tool_name:
                self.remembered.setdefault(session_id, set()).add(tool_name)
                logger.info("approval remembered session=%s tool=%s", session_id, tool_name)
        future.set_result((approved is True, reason))

    def pending_list(self):
        """Snapshot of the requests still waiting for a decision."""
        return [{"id": identity, "sessionId": self.owners.get(identity, ""),
                 "kind": self.kinds.get(identity, "approve")}
                for identity, future in self.pending.items() if not future.done()]

    def cancel_session(self, session_id: str):
        """Deny everything a session is waiting on so its run can unwind."""
        cancelled = []
        for identity, owner in list(self.owners.items()):
            if owner != session_id:
                continue
            future = self.pending.get(identity)
            if future is None or future.done():
                continue
            future.set_result((False, CANCEL_REASON))
            cancelled.append(identity)
        self.asked.pop(session_id, None)
        return cancelled

    def forget_session(self, session_id: str):
        """Session-scoped grants die with the session they were given to."""
        return bool(self.remembered.pop(session_id, None))
