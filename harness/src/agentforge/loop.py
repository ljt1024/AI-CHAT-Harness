import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from agentforge.context import DEFAULT_WINDOW, estimate_tokens, history_boundary
from agentforge.events import EventBus
from agentforge.gateway.registry import ModelGateway
from agentforge.gateway.base import ModelOutputError
from agentforge.models import AgentDefinition, Cost, RunResult, ToolResult, new_id
from agentforge.prompts import with_response_language
from agentforge.store import MongoDBStore
from agentforge.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


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
        started_at = time.perf_counter()
        messages, cost, start = [], Cost(), 0
        output, reason = "", "max_turns"

        async def emit(kind, **data):
            await events.emit({"type": kind, "sessionId": session_id, "turnId": turn_id,
                               "trace_id": trace_id, **data})

        async def emit_context():
            # The UI shows how full the live transcript is. It is measured after the
            # compact step, so what is displayed is what the next request will carry.
            window = self.context_manager.window if self.context_manager else DEFAULT_WINDOW
            reading = {"tokens": estimate_tokens(messages), "window": window}
            await emit("context", contextTokens=reading["tokens"], contextWindow=window)
            return reading

        async def snapshot(index):
            await self.store.checkpoint(session_id, {
                "messages": messages, "index": index, "cost": cost.model_dump(),
                "trace_id": trace_id, "turnId": turn_id, "agent_id": agent.id,
                "task": task, "fingerprint": last_fingerprint, "idle": idle,
                "tool_calls": tool_count, "output_recoveries": output_recoveries,
                "completed": reason == "completed"})

        ctx = None
        last_fingerprint, idle, tool_count, output_recoveries = "", 0, 0, 0
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
                task = state["task"]
                cost, trace_id, turn_id = Cost(**state["cost"]), state["trace_id"], state["turnId"]
                last_fingerprint, idle = state.get("fingerprint", ""), state.get("idle", 0)
                tool_count = state.get("tool_calls", 0)
                output_recoveries = state.get("output_recoveries", 0)
            else:
                if session.get("lastTurnId") == turn_id:
                    raise ValueError("This turn is already committed; use a new turnId.")
                messages = [{"role": "system", "content": agent.system_prompt},
                            *session.get("history", []), {"role": "user", "content": task}]
            tools: ToolRegistry = self.tools_factory(session_id)
            schemas = tools.schemas(agent.tools)
            tool_context = {"role": "system", "content":
                "Current runtime capabilities (authoritative for this request):\n" +
                "\n".join(f"- {s['function']['name']}: {s['function']['description']}" for s in schemas) +
                "\nUse the tool schemas supplied with this request. Earlier assistant messages about "
                "missing tools or permissions may be outdated; they are not capability restrictions. "
                "When a current tool supports the user's request, call it instead of repeating a "
                "historical refusal. Approval requirements still apply. Do not claim success without "
                "a successful tool result."}
            await self.store.put("sessions", {**session, "agent_id": agent.id, "turnId": turn_id,
                                               "status": "running"})
            await emit("turn_started", memoryMessages=len(messages))
            logger.info("run started session=%s agent=%s trace=%s turn=%s messages=%d tools=%d resume=%s",
                        session_id, agent.id, trace_id, turn_id, len(messages), len(agent.tools), resume)
            for index in range(start, agent.stop_conditions.max_turns):
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                ctx = TurnContext(agent, messages, index, cost, model_calls=index, tool_calls=tool_count,
                                  metadata={"session_id": session_id})
                if self.context_manager:
                    messages = await self.context_manager.prepare(
                        messages, self.gateway, agent.model, cost, agent.stop_conditions.budget,
                        emit=emit)
                    ctx.messages = messages
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                if self.middleware:
                    ctx = await self.middleware.on_turn_start(ctx)
                    messages = ctx.messages
                text, calls, chunks = "", [], []
                # Refresh capabilities after history without storing another manifest in the session.
                remaining = agent.stop_conditions.max_turns - index
                current_tools = tool_context
                if remaining <= 3:
                    current_tools = {**tool_context, "content": tool_context["content"] +
                        f"\nRuntime turn budget: {remaining} model response(s) remaining, INCLUDING this one. "
                        "Finish existing work and return the final answer within this budget. "
                        "Do not expand scope or repeat checks. If this is the last response, return "
                        "an honest final summary now instead of calling more tools. State what was "
                        "actually written and verified, and disclose anything incomplete or untested."}
                model_messages = with_response_language([*messages, current_tools], task)
                model_error = None
                try:
                    async for chunk in self.gateway.chat(model_messages, schemas, agent.model):
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
                except ModelOutputError as exc:
                    model_error = exc
                if model_error:
                    await self.store.put("turns", {"id": f"{trace_id}:{index}", "trace_id": trace_id,
                        "index": index, "messages": messages, "model_messages": model_messages,
                        "enabled_tools": agent.tools, "chunks": chunks, "middleware": ctx.metadata,
                        "error_code": model_error.code, "finish_reason": model_error.finish_reason})
                    logger.warning("Rejected model output: session=%s trace=%s index=%s code=%s finish=%s",
                                   session_id, trace_id, index, model_error.code, model_error.finish_reason)
                    if (output_recoveries >= 2 or index + 1 >= agent.stop_conditions.max_turns
                            or cost.usd >= agent.stop_conditions.budget):
                        raise model_error
                    output_recoveries += 1
                    # No calls from the incomplete response are executed or added to history.
                    messages.append({"role": "user", "content":
                        "Runtime feedback: your last model response was truncated or had invalid JSON "
                        "tool arguments. NONE of its tool calls were executed. Do not repeat any earlier "
                        "successful calls. Produce a SMALLER complete response with valid JSON arguments. "
                        "For long code, write separate HTML/CSS/JS files or write_file chunks under 6000 "
                        "characters each; use append=true only for subsequent chunks of an existing file. "
                        "Do not put a whole large application in run_command or one write_file call. "
                        "Keep answering in the original user's requested language."})
                    message = ("模型输出不完整，正在尝试分段生成。" if any("\u3400" <= c <= "\u9fff" for c in task)
                               else "Model output was incomplete; retrying with smaller chunks.")
                    await emit("model_recovery", attempt=output_recoveries, reason=model_error.code,
                               message=message, step_id=f"recovery:{trace_id}:{index}")
                    if self.middleware:
                        await self.middleware.on_turn_end(ctx)
                    await snapshot(index + 1)
                    continue
                assistant = {"role": "assistant", "content": text or None}
                if calls:
                    assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                        "name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls]
                messages.append(assistant)
                await self.store.put("turns", {"id": f"{trace_id}:{index}", "trace_id": trace_id,
                                               "index": index, "messages": messages[:-1],
                                               "model_messages": model_messages,
                                               "enabled_tools": agent.tools, "chunks": chunks,
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
                        if not permitted:
                            result = ToolResult(
                                ok=False, error="Permission denied. This action was not executed. Do not repeat the "
                                "denied action unless the user changes their decision; use a read-only alternative "
                                "or report the blocker.")
                            logger.info("tool denied session=%s trace=%s index=%d name=%s",
                                        session_id, trace_id, index, call.name)
                        else:
                            tool_started = time.perf_counter()
                            result = await tools.execute(call.name, call.arguments)
                            # Arguments and output are deliberately absent: they
                            # carry user data and can carry credentials.
                            logger.info("tool session=%s trace=%s index=%d name=%s ok=%s %.0fms",
                                        session_id, trace_id, index, call.name, result.ok,
                                        (time.perf_counter() - tool_started) * 1000)
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
                await emit_context()
                if (index + 1) % 5 == 0:
                    await snapshot(index + 1)
                if cost.usd >= agent.stop_conditions.budget:
                    reason = "budget"
                    break
                if idle >= agent.stop_conditions.idle_limit * 2:
                    reason = "idle"
                    break
            if not output and reason != "completed":
                chinese = any("\u3400" <= c <= "\u9fff" for c in task)
                output = ("本次执行已达到轮次、预算或空转限制，任务尚未确认完成。已成功执行的操作不会自动重放，请查看执行记录后继续。"
                          if chinese else "Execution reached a turn, budget or idle limit before completion. "
                          "Successful actions are not replayed automatically. Review the trace before continuing.")
            result = RunResult(trace_id=trace_id, session_id=session_id, turn_id=turn_id,
                               output=output, stop_reason=reason, cost=cost)
            await self.store.put("traces", {"id": trace_id, "sessionId": session_id,
                                           "agent_id": agent.id, **result.model_dump()})
            # The last turn ends the loop early when the model answers without tools,
            # so publish one final reading that includes the answer itself.
            reading = await emit_context()
            # Every orderly ending commits what was learned, including budget, turn and
            # idle stops, so continuing the conversation does not start from nothing.
            # Crashes and cancellations leave history alone (ADR-0003); they resume from
            # the latest checkpoint instead. The boundary keeps tool pairs intact when
            # the last response was cut off mid-flight.
            committed = messages[1:history_boundary(messages)]
            # The reading is persisted with the transcript it describes: a refreshed page
            # has no event stream to replay, so this is where the composer gauge refills.
            updated = {**session, "agent_id": agent.id, "turnId": turn_id, "status": reason,
                       "history": committed or session.get("history", []), "context": reading}
            if reason == "completed":
                updated["lastTurnId"] = turn_id
            await self.store.put("sessions", updated)
            logger.info("run finished session=%s trace=%s stop=%s turns=%d usd=%.4f tokens=%d/%d %.0fms",
                        session_id, trace_id, reason, index + 1, cost.usd, cost.tokens_in,
                        cost.tokens_out, (time.perf_counter() - started_at) * 1000)
            await emit("done", result=result.model_dump())
            return result
        except BaseException as exc:
            if isinstance(exc, (Exception, asyncio.CancelledError)):
                # A ValueError is the runtime's own refusal and a cancellation is
                # a normal stop, so neither deserves a traceback; anything else is
                # a bug and does.
                expected = isinstance(exc, (ValueError, ModelOutputError, asyncio.CancelledError))
                logger.log(logging.INFO if isinstance(exc, asyncio.CancelledError) else logging.WARNING,
                           "run stopped session=%s trace=%s error=%s: %s", session_id, trace_id,
                           type(exc).__name__, exc, exc_info=not expected)
                if type(exc).__name__ == "MiddlewareRejected":
                    await emit("middleware_rejected", message=str(exc))
                if self.middleware and ctx:
                    await self.middleware.on_error(ctx, exc)
                await self.store.put("traces", {"id": trace_id, "sessionId": session_id,
                    "agent_id": agent.id, "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                    "cost": cost.model_dump(), "error": type(exc).__name__,
                    "error_code": exc.code if isinstance(exc, ModelOutputError) else "run_failed"})
                current = await self.store.get("sessions", session_id)
                if current:
                    await self.store.put("sessions", {**current, "status": "error"})
                chinese = any("\u3400" <= c <= "\u9fff" for c in task)
                if isinstance(exc, ModelOutputError):
                    message = ("模型返回了不完整或无效的输出，已停止本次生成。本次不完整响应中的工具调用均未执行。请将任务拆分为较小文件后重试。"
                               if chinese else "The model returned incomplete or invalid output. No tools "
                               "from the incomplete response were executed. Retry with smaller files.")
                elif isinstance(exc, ValueError):
                    # The runtime raises these itself, so the text already says what to
                    # do: wrong session, committed turn, no checkpoint to resume from.
                    message = str(exc)
                else:
                    message = ("执行失败，请重试；如仍失败，请提供执行记录编号以便排查。" if chinese
                               else "Run failed. Inspect the server logs and configuration.")
                await emit("error", message=message,
                           code=exc.code if isinstance(exc, ModelOutputError) else "run_failed")
            raise
        finally:
            self.active.discard(session_id)


async def run_agent(agent: AgentDefinition, task: str, session_id: str, events: EventBus) -> RunResult:
    if events.runtime is None:
        raise ValueError("Attach a Runtime to the EventBus before running an agent.")
    return await events.runtime.run(agent, task, session_id, events)
