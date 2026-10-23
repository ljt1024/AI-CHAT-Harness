import asyncio

from agentforge.models import new_id
from agentforge.sandbox.tools import SandboxTool

CANCEL_REASON = "Approval cancelled: the run was stopped."


class ApprovalManager:
    def __init__(self, store, timeout=300):
        self.store, self.timeout = store, timeout
        self.pending: dict[str, asyncio.Future] = {}
        # Approval id -> session that requested it. Lets a stopped run release
        # whatever is waiting on it instead of hanging until the timeout.
        self.owners: dict[str, str] = {}

    async def check(self, agent, tool, call, emit, session_id=""):
        levels = {"auto": 1, "rule": 2, "confirm": 3}
        # Agent configuration can escalate a tool's required permission, never lower it.
        level = max(levels[tool.permission], levels[agent.permissions.get(tool.name, "auto")])
        if level == 1:
            return True
        if level == 2 and tool.name == "run_command" and isinstance(tool, SandboxTool):
            # Trust the real Docker isolation boundary, not command-text heuristics.
            # Host shell, installers and remote MCP tools never enter this rule.
            return tool.sandbox.alive
        if level == 2 and tool.name == "write_file" and tool.source == "builtin":
            try:
                path = tool.workspace.path(call.arguments["path"])
                if any(part.startswith(".") for part in path.relative_to(tool.workspace.root).parts):
                    return False
                return True
            except (ValueError, KeyError):
                return False
        identity = new_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        if session_id:
            self.owners[identity] = session_id
        record = {"id": identity, "tool_call_id": call.id, "tool": call.name,
                  "arguments": call.arguments, "status": "pending"}
        if session_id:
            record["sessionId"] = session_id
        try:
            await self.store.put("approvals", record)
            await emit("await_approval", call_id=identity, tool_call_id=call.id,
                       tool=call.name, arguments=call.arguments)
            try:
                approved, reason = await asyncio.wait_for(future, self.timeout)
            except TimeoutError:
                approved, reason = False, "Approval timed out; denied by default."
            await self.store.put("approvals", {**record,
                "status": "approved" if approved else "denied", "reason": reason})
            await emit("approval_resolved", call_id=identity, approved=approved, reason=reason)
            return approved
        finally:
            self.pending.pop(identity, None)
            self.owners.pop(identity, None)

    def resolve(self, call_id: str, approved: bool, reason: str = ""):
        future = self.pending.get(call_id)
        if future is None or future.done():
            raise ValueError("Approval is expired or already resolved.")
        future.set_result((approved is True, reason))

    def pending_list(self):
        """Snapshot of the requests still waiting for a decision."""
        return [{"id": identity, "sessionId": self.owners.get(identity, "")}
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
        return cancelled
