from agentforge.context import ContextManager, estimate_tokens, truncate_tool_output
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


def test_truncation_and_multilingual_estimate():
    assert len(truncate_tool_output("x" * 5000)) <= 4000
    assert "Truncated" in truncate_tool_output("x" * 5000)
    assert estimate_tokens([{"role": "user", "content": "中文"}]) > 2
