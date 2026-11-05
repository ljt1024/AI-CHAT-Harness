"""The SSE event names are a contract between two languages, so pin both ends.

The runtime emits Python names, `legacy_event()` rewrites a few of them, and the
browser switches on a hand-written TypeScript union. Nothing makes the two sides
agree at compile time: a renamed backend event used to reach the UI as an event no
handler matched, which looks like a feature that silently stopped working.
"""

import re
from pathlib import Path

import pytest

from agentforge.api import LEGACY_EVENT_NAMES, legacy_event
from agentforge.events import INTERNAL_EVENTS, WIRE_EVENTS

TYPES_TS = Path(__file__).resolve().parents[3] / "src/features/agents/types.ts"


def declared_by_the_ui() -> set[str]:
    source = TYPES_TS.read_text(encoding="utf-8")
    # The union spans several lines and its members contain semicolons, so slice to the
    # next declaration and read the discriminant of every member.
    start = source.index("export type AgentEvent =")
    union = source[start:source.index("\nexport ", start)]
    return set(re.findall(r"type: '([a-z_]+)'", union))


def test_ui_declares_exactly_what_the_backend_can_send():
    if not TYPES_TS.exists():
        pytest.skip("frontend sources are not part of this checkout")
    assert declared_by_the_ui() == set(WIRE_EVENTS)


def test_only_internal_names_are_rewritten():
    assert set(LEGACY_EVENT_NAMES) == set(INTERNAL_EVENTS)
    # A rewrite target that is not registered would ship an untyped event.
    assert set(LEGACY_EVENT_NAMES.values()) <= WIRE_EVENTS


def test_rewrite_keeps_the_payload_it_does_not_rename():
    # `start` and `delta` are pure renames; their fields must survive untouched.
    assert legacy_event({"type": "turn_started", "memoryMessages": 3}, "task") == \
        {"type": "start", "memoryMessages": 3}
    assert legacy_event({"type": "content", "text": "hi"}, "task") == \
        {"type": "delta", "text": "hi"}
    # Every tool row the UI renders comes from one of these two rewrites.
    action = legacy_event({"type": "tool_call", "call": {"id": "c1", "name": "read_file",
                                                         "arguments": {"path": "a"}}}, "task")
    observation = legacy_event({"type": "tool_result", "call_id": "c1",
                                "result": {"ok": True, "output": "done", "error": None}}, "task")
    assert (action["type"], action["step"]["phase"], action["step"]["status"]) == ("step", "action", "running")
    assert (observation["type"], observation["step"]["phase"], observation["step"]["status"]) == \
        ("step", "observation", "completed")


async def test_unknown_event_names_are_rejected_at_the_emit_site():
    from agentforge.events import EventBus

    with pytest.raises(ValueError, match="Register it in agentforge/events.py"):
        await EventBus().emit({"type": "tun_started"})
