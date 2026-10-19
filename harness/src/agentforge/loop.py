import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from agentforge.events import EventBus
from agentforge.gateway.registry import ModelGateway
from agentforge.models import AgentDefinition, Cost, RunResult, ToolResult, new_id
from agentforge.store import MongoDBStore
from agentforge.tools.base import ToolRegistry


class SessionBusy(ValueError):
    pass


@dataclass
class TurnContext:
    agent: AgentDefinition
    messages: list[dict]
    index: int
    cost: Cost
    model_calls: int = 0
    tool_calls: int = 0
    metadata: dict = field(default_factory=dict)


class Runtime:
    def __init__(self, gateway: ModelGateway, store: MongoDBStore, tools_factory):
        self.gateway, self.store, self.tools_factory = gateway, store, tools_factory
        self.active: set[str] = set()
        self.middleware = None
        self.permission_check = None
        self.context_manager = None

    async def run(self, agent: AgentDefinition, task: str, session_id: str, events: EventBus,
                  turn_id: str | None = None, resume: bool = False) -> RunResult:
        if session_id in self.active:
            raise SessionBusy("Session already has an active run.")
        self.active.add(session_id)
        trace_id, turn_id = new_id(), turn_id or new_id()
        messages, cost, start = [], Cost(), 0
        output, reason = "", "max_turns"

        async def emit(kind, **data):
            await events.emit({"type": kind, "sessionId": session_id, "turnId": turn_id,
                               "trace_id": trace_id, **data})

        async def snapshot(index):
            await self.store.checkpoint(session_id, {
                "messages": messages, "index": index, "cost": cost.model_dump(),
                "trace_id": trace_id, "turnId": turn_id, "agent_id": agent.id,
                "task": task, "fingerprint": last_fingerprint, "idle": idle,
                "tool_calls": tool_count, "completed": reason == "completed"})

        ctx = None
        last_fingerprint, idle, tool_count = "", 0, 0
        try:
            session = await self.store.get("sessions", session_id) or {"id": session_id, "sessionId": session_id}
            if session.get("agent_id") not in {None, agent.id}:
                raise ValueError("A session cannot switch agents; create a new session.")
            if resume:
                checkpoint = await self.store.latest_checkpoint(session_id)
                if not checkpoint or checkpoint["state"]["agent_id"] != agent.id:
                    raise ValueError("No matching checkpoint exists for this session.")
                state = checkpoint["state"]
                if state.get("completed"):
                    raise ValueError("This checkpoint is already complete; start a new turn.")
                messages, start = state["messages"], state["index"]
                cost, trace_id, turn_id = Cost(**state["cost"]), state["trace_id"], state["turnId"]
                last_fingerprint, idle = state.get("fingerprint", ""), state.get("idle", 0)
                tool_count = state.get("tool_calls", 0)
            else:
                if session.get("lastTurnId") == turn_id:
                    raise ValueError("This turn is already committed; use a new turnId.")
                messages = [{"role": "system", "content": agent.system_prompt},
                            *session.get("history", []), {"role": "user", "content": task}]
            tools: ToolRegistry = self.tools_factory(session_id)
            schemas = tools.schemas(agent.tools)
            await self.store.put("sessions", {**session, "agent_id": agent.id, "turnId": turn_id,
                                               "status": "running"})
            await emit("turn_started", memoryMessages=len(messages))
            for index in range(start, agent.stop_conditions.max_turns):
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                ctx = TurnContext(agent, messages, index, cost, model_calls=index, tool_calls=tool_count)
                if self.context_manager:
                    messages = await self.context_manager.prepare(
                        messages, self.gateway, agent.model, cost, agent.stop_conditions.budget)
                    ctx.messages = messages
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                if self.middleware:
                    ctx = await self.middleware.on_turn_start(ctx)
                    messages = ctx.messages
                text, calls, chunks = "", [], []
                async for chunk in self.gateway.chat(messages, schemas, agent.model):
                    chunks.append(chunk.model_dump())
                    if chunk.type == "content":
                        text += chunk.text
                        await emit("content", text=chunk.text)
                    elif chunk.type == "tool_call":
                        calls.append(chunk.call)
                    elif chunk.type == "usage":
                        cost.tokens_in += chunk.tokens_in
                        cost.tokens_out += chunk.tokens_out
                        cost.usd += chunk.usd
                assistant = {"role": "assistant", "content": text or None}
                if calls:
                    assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                        "name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls]
                messages.append(assistant)
                await self.store.put("turns", {"id": f"{trace_id}:{index}", "trace_id": trace_id,
                                               "index": index, "messages": messages[:-1], "chunks": chunks,
                                               "middleware": ctx.metadata})
                if not calls:
                    output, reason = text, "completed"
                    if self.middleware:
                        await self.middleware.on_turn_end(ctx)
                    await snapshot(index + 1)
                    break
                results = []
                for call in calls:
                    if call.name not in agent.tools:
                        result = ToolResult(ok=False, error="Tool is not enabled for this agent.")
                    elif cost.usd >= agent.stop_conditions.budget:
                        result = ToolResult(ok=False, error="Budget exhausted. Tool was not executed.")
                    else:
                        if self.middleware:
                            call = await self.middleware.on_tool_call(ctx, call)
                        await emit("tool_call", call=call.model_dump())
                        permitted = self.permission_check and await self.permission_check(
                            agent, tools.tools[call.name], call, emit)
                        if self.permission_check is None:
                            permitted = tools.tools[call.name].permission == "auto"
                        result = await tools.execute(call.name, call.arguments) if permitted else ToolResult(
                            ok=False, error="Permission denied. Request approval or choose a read-only tool.")
                    if self.middleware:
                        result = await self.middleware.on_tool_result(ctx, result)
                    await self.store.put("tool_calls", {"id": f"{trace_id}:{index}:{call.id}",
                        "call_id": call.id, "trace_id": trace_id, "index": index,
                        "call": call.model_dump(), "result": result.model_dump()})
                    await emit("tool_result", call_id=call.id, result=result.model_dump())
                    results.append({"name": call.name, "args": call.arguments, "result": result.model_dump()})
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result.model_dump())})
                fingerprint = hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()
                idle = idle + 1 if fingerprint == last_fingerprint else 1
                last_fingerprint = fingerprint
                if idle == agent.stop_conditions.idle_limit:
                    messages.append({"role": "user", "content":
                        "Repeated tool calls made no progress. Reflect and try a different approach."})
                    await emit("reflection", consecutive=idle)
                if self.middleware:
                    await self.middleware.on_turn_end(ctx)
                    tool_count = ctx.tool_calls
                if (index + 1) % 5 == 0:
                    await snapshot(index + 1)
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                if idle >= agent.stop_conditions.idle_limit * 2:
                    reason = "idle"
                    break
            result = RunResult(trace_id=trace_id, session_id=session_id, turn_id=turn_id,
                               output=output, stop_reason=reason, cost=cost)
            await self.store.put("traces", {"id": trace_id, "sessionId": session_id,
                                           "agent_id": agent.id, **result.model_dump()})
            updated = {**session, "agent_id": agent.id, "turnId": turn_id, "status": reason}
            if reason == "completed":
                updated.update(history=messages[1:], lastTurnId=turn_id)
            await self.store.put("sessions", updated)
            await emit("done", result=result.model_dump())
            return result
        except BaseException as exc:
            if isinstance(exc, (Exception, asyncio.CancelledError)):
                if type(exc).__name__ == "MiddlewareRejected":
                    await emit("middleware_rejected", message=str(exc))
                if self.middleware and ctx:
                    await self.middleware.on_error(ctx, exc)
                await self.store.put("traces", {"id": trace_id, "sessionId": session_id,
                    "agent_id": agent.id, "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                    "cost": cost.model_dump(), "error": type(exc).__name__})
                current = await self.store.get("sessions", session_id)
                if current:
                    await self.store.put("sessions", {**current, "status": "error"})
                await emit("error", message="Run failed. Inspect the server logs and configuration.")
            raise
        finally:
            self.active.discard(session_id)


async def run_agent(agent: AgentDefinition, task: str, session_id: str, events: EventBus) -> RunResult:
    if events.runtime is None:
        raise ValueError("Attach a Runtime to the EventBus before running an agent.")
    return await events.runtime.run(agent, task, session_id, events)
