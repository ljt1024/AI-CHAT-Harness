from agentforge.context import ContextManager, estimate_tokens, history_boundary, truncate_tool_output
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
