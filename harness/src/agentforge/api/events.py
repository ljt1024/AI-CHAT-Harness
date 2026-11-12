"""The legacy SSE envelope.

The browser's first protocol (AI-CHAT 2.1.1) is still what the chat page speaks,
so the harness translates its own events into that shape at the edge. The
mapping lives in its own module because `test_event_contract` checks it against
WIRE_EVENTS, and a table is easier to check than scattered reassignments.
"""
import asyncio
import json


def legacy_result(result, task):
    return {"id": result["trace_id"], "sessionId": result["session_id"], "input": task,
            "output": result["output"], "steps": [], "memoryMessages": 0,
            "cost": result["cost"], "stop_reason": result["stop_reason"],
            "artifacts": result.get("artifacts", [])}


# Internal name -> name the browser receives. Kept as a table rather than scattered
# reassignments so the mapping is checkable against WIRE_EVENTS (test_event_contract).
LEGACY_EVENT_NAMES = {"turn_started": "start", "content": "delta", "tool_call": "step",
                      "tool_result": "step", "model_recovery": "step"}


def legacy_event(event, task):
    event = event.copy()
    kind = event["type"]
    event["type"] = LEGACY_EVENT_NAMES.get(kind, kind)
    if kind == "tool_call":
        call = event["call"]
        event["step"] = {"id": call["id"], "toolCallId": call["id"], "phase": "action",
                         "status": "running", "agentId": call["name"],
                         "output": f"{call['name']} {json.dumps(call['arguments'])}"}
    elif kind == "tool_result":
        result = event["result"]
        event["step"] = {"id": event["call_id"], "toolCallId": event["call_id"],
                         "phase": "observation", "status": "completed" if result["ok"] else "failed",
                         "output": result["output"] or result["error"] or ""}
    elif kind == "model_recovery":
        event["step"] = {"id": event["step_id"], "phase": "thought",
                         "status": "completed", "output": event["message"]}
    elif kind == "done":
        event["result"] = legacy_result(event["result"], task)
    return event


async def event_frames(bus, task="", legacy=False, after=0):
    cursor = after
    while True:
        async with bus.condition:
            try:
                await asyncio.wait_for(bus.condition.wait_for(lambda: len(bus.events) > cursor), 15)
            except TimeoutError:
                batch = []
            else:
                batch = bus.events[cursor:]
        if not batch:
            yield ": heartbeat\n\n"
        for event in batch:
            cursor += 1
            if legacy:
                event = legacy_event(event, task)
            yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] in {"done", "error"}:
                return
