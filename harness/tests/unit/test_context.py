import json

import pytest

from agentforge.context import (ContextManager, clip_summary, estimate_tokens, history_boundary,
                                truncate_tool_output)
from agentforge.gateway.base import ContentDelta, UsageReport
from agentforge.models import Cost, ModelRef


class Summarizer:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, model_ref):
        self.calls += 1
        yield ContentDelta(text="Goal: finish task. Next: verify output.")
        yield UsageReport(tokens_in=100, tokens_out=20, usd=0.001)


async def test_compaction_batches_and_tool_pair_integrity():
    history = [{"role": "system", "content": "system"}]
    for _ in range(10):
        history += [{"role": "user", "content": "x" * 200},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]},
                    {"role": "tool", "tool_call_id": "c", "content": "result"}]
    history.append({"role": "user", "content": "continue"})
    summarizer, cost = Summarizer(), Cost()
    result = await ContextManager(window=1000, batch_chars=500).prepare(history, summarizer, ModelRef(), cost)
    assert summarizer.calls > 1
    assert cost.tokens_in == 100 * summarizer.calls
    assert estimate_tokens(result) < 700
    assert result[0] == history[0]
    assert result[-1] == history[-1]
    assert result[2]["role"] != "tool"


async def test_compaction_reports_what_it_folded_away():
    history = [{"role": "system", "content": "system"}]
    for index in range(6):
        history += [{"role": "user", "content": "x" * 200},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}"}]},
                    {"role": "tool", "tool_call_id": f"c{index}", "content": "result"}]
    history.append({"role": "user", "content": "continue"})
    seen = []

    async def emit(kind, **data):
        seen.append((kind, data))

    result = await ContextManager(window=1000, batch_chars=500).prepare(
        history, Summarizer(), ModelRef(), Cost(), emit=emit)
    assert seen and seen[0][0] == "memory"
    summarized, recent = seen[0][1]["summarizedMessages"], seen[0][1]["recentMessages"]
    # The folded-away messages and the retained ones account for the whole transcript.
    assert summarized + recent == len(history)
    # Retained count includes the message that carries the summary itself.
    assert len(result) == recent + 1


def test_history_boundary_never_splits_a_tool_pair():
    complete = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
                {"role": "tool", "tool_call_id": "a", "content": "ok"},
                {"role": "user", "content": "next"}]
    assert history_boundary(complete) == len(complete)
    orphaned = complete + [{"role": "assistant", "content": None, "tool_calls": [{"id": "b"}]}]
    committed = orphaned[1:history_boundary(orphaned)]
    # The cutoff leaves the unanswered call out entirely rather than half-persisting it.
    assert committed == complete[1:]
    pending = set()
    for message in committed:
        for call in message.get("tool_calls") or []:
            pending.add(call["id"])
        if message["role"] == "tool":
            pending.discard(message["tool_call_id"])
    assert not pending


def test_truncation_and_multilingual_estimate():
    assert len(truncate_tool_output("x" * 5000)) <= 4000
    assert "Truncated" in truncate_tool_output("x" * 5000)
    assert estimate_tokens([{"role": "user", "content": "中文"}]) > 2


def test_clip_summary_keeps_a_clean_break_and_the_budget():
    assert clip_summary("short", 100) == "short"
    paragraph = "第一段讲目标和约束。" * 20 + "\n\n" + "第二段讲下一步。" * 20
    clipped = clip_summary(paragraph, 150)
    assert len(clipped) <= 150
    # Ends on a real boundary instead of slicing a word in half.
    assert clipped.endswith("。")


def history_of(size: int) -> list[dict]:
    messages = [{"role": "system", "content": "system"}]
    for index in range(size):
        messages += [{"role": "user", "content": "x" * 200},
                     {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}"}]},
                     {"role": "tool", "tool_call_id": f"c{index}", "content": "result"}]
    messages.append({"role": "user", "content": "continue"})
    return messages


async def test_an_over_long_summary_is_clipped_instead_of_failing_the_run():
    """A real transcript was killed by 6016 characters against a 6000 budget."""
    class Verbose:
        async def chat(self, messages, tools, model_ref):
            yield ContentDelta(text="要点。" * 200)

    manager = ContextManager(window=1000, batch_chars=500, summary_chars=200)
    result = await manager.prepare(history_of(6), Verbose(), ModelRef(), Cost())
    prefix, body = result[1]["content"].split("\n", 1)
    assert prefix == "Task state summary:" and len(body) <= 200 and body.endswith("。")


async def test_an_empty_summary_retries_the_batch_in_halves():
    class EmptyWhenLarge:
        def __init__(self):
            self.batches: list[int] = []

        async def chat(self, messages, tools, model_ref):
            batch = json.loads(messages[1]["content"])["historical_text"]
            self.batches.append(len(batch))
            if len(batch) > 400:
                return
            yield ContentDelta(text="部分摘要。")

    summarizer = EmptyWhenLarge()
    manager = ContextManager(window=1000, batch_chars=500, summary_chars=200)
    result = await manager.prepare(history_of(6), summarizer, ModelRef(), Cost())
    assert summarizer.batches[0] > 400
    assert any(size <= 400 for size in summarizer.batches[1:])
    assert "部分摘要" in result[1]["content"]


async def test_a_model_that_never_summarises_says_what_to_do():
    class Mute:
        async def chat(self, messages, tools, model_ref):
            return
            yield  # pragma: no cover - an empty async generator

    manager = ContextManager(window=1000, batch_chars=500, summary_chars=200)
    with pytest.raises(ValueError, match="empty context summary"):
        await manager.prepare(history_of(6), Mute(), ModelRef(), Cost())


async def test_compaction_keeps_the_largest_recent_tail_the_budget_allows():
    """"Three messages" is not a unit of context: it is one short turn.

    A long task's latest turns carry the constraints the summary only paraphrases,
    so the tail is sized by tokens -- as much as a quarter of the window -- and
    only falls back to the minimum when even that is too big.
    """
    history = [{"role": "system", "content": "system"}]
    for index in range(30):
        history += [{"role": "user", "content": f"step {index} " + "x" * 60},
                    {"role": "assistant", "content": "done"}]
    result = await ContextManager(window=1000, batch_chars=2000).prepare(
        history, Summarizer(), ModelRef(), Cost())
    retained = len(result) - 2
    assert retained > 3, "the budget, not a fixed count, decides how much stays verbatim"
    assert estimate_tokens(result) < 700
    assert result[-1] == history[-1]


async def test_an_oversized_tail_still_compacts_to_the_minimum():
    """When the last few messages alone blow the budget, keep the least possible."""
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "start"}]
    for index in range(6):
        history += [{"role": "user", "content": "x" * 400},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}"}]},
                    {"role": "tool", "tool_call_id": f"c{index}", "content": "y" * 400}]
    result = await ContextManager(window=1000, batch_chars=2000).prepare(
        history, Summarizer(), ModelRef(), Cost())
    assert len(result) == 5, "system, summary, then the minimum tail: request, call, result"
    assert estimate_tokens(result) < 700
