import asyncio

# Names the runtime emits before the wire rewrite. `legacy_event()` in api.py maps each
# of them to a name the browser already knows, see LEGACY_EVENT_NAMES.
INTERNAL_EVENTS = frozenset({"turn_started", "content", "tool_call", "tool_result", "model_recovery"})

# Everything `POST /api/agents/run` can put on the wire. This is the contract the
# `AgentEvent` union in `src/features/agents/types.ts` mirrors, and the two are held
# together by `tests/unit/test_event_contract.py`.
WIRE_EVENTS = frozenset({
    "approval_resolved", "artifact", "await_approval", "await_input", "context", "delta",
    "done", "error", "memory", "middleware_rejected", "reflection", "skill_deleted",
    "skill_installed", "skill_loaded", "start", "step",
})

KNOWN_EVENTS = INTERNAL_EVENTS | WIRE_EVENTS


class EventBus:
    def __init__(self, runtime=None):
        self.runtime = runtime
        self.events: list[dict] = []
        self.condition = asyncio.Condition()

    async def emit(self, event: dict):
        # An unregistered name would reach the browser as an event no handler matches,
        # so it would vanish without a trace. Fail at the emit site instead.
        if event.get("type") not in KNOWN_EVENTS:
            raise ValueError(f"Unknown event type {event.get('type')!r}. Register it in "
                             f"agentforge/events.py and in src/features/agents/types.ts.")
        async with self.condition:
            self.events.append(event.copy())
            self.condition.notify_all()

    async def stream(self, after: int = 0):
        cursor = after
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: len(self.events) > cursor)
                batch = self.events[cursor:]
            for event in batch:
                cursor += 1
                yield event
                if event["type"] in {"done", "error"}:
                    return
