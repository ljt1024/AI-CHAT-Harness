"""How much of a tool result survives, and where the ceiling comes from.

A remote service tends to answer with one document, so the local 4k default
would cut it mid-string; a local tool gains nothing from a bigger slice. These
tests pin both halves, plus the one policy exemption that goes with the
catalogue search added today.
"""
from pathlib import Path

from agentforge.middleware import ToolsSummarization
from agentforge.models import ToolResult
from agentforge.policy import InterventionPolicy
from agentforge.tools.base import DEFAULT_OUTPUT_LIMIT, ToolRegistry


class Stub:
    """A tool whose only job is to return a result of a given size."""
    source = "builtin"

    def __init__(self, name: str, output: str, limit: int | None = None,
                 error: str | None = None):
        self.name, self.description, self.schema = name, name, {"type": "object"}
        self.permission = "auto"
        self.output, self.limit, self.error = output, limit, error

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output=self.output, error=self.error, limit=self.limit)


async def test_remote_result_keeps_its_own_ceiling():
    registry = ToolRegistry()
    registry.register(Stub("mcp_demo__search", "x" * 12000, limit=16000))
    result = await registry.execute("mcp_demo__search", {})
    assert len(result.output) == 12000, "the registry must not re-clip at the local default"


async def test_local_result_is_still_clipped_to_the_default():
    registry = ToolRegistry()
    registry.register(Stub("grep", "x" * 12000))
    result = await registry.execute("grep", {})
    assert len(result.output) == DEFAULT_OUTPUT_LIMIT


async def test_middleware_honours_the_tool_ceiling():
    layer = ToolsSummarization()
    kept = await layer.on_tool_result(None, ToolResult(ok=True, output="x" * 12000, limit=16000))
    clipped = await layer.on_tool_result(None, ToolResult(ok=True, output="x" * 12000))
    assert len(kept.output) == 12000
    assert len(clipped.output) == DEFAULT_OUTPUT_LIMIT


async def test_a_remote_ceiling_does_not_unbound_errors():
    layer = ToolsSummarization()
    result = await layer.on_tool_result(
        None, ToolResult(ok=False, output="", error="e" * 9000, limit=16000))
    assert len(result.error) == DEFAULT_OUTPUT_LIMIT


def test_catalogue_search_runs_without_a_card():
    """It declares no annotation, so the policy file is what keeps the run moving."""
    policy = InterventionPolicy.load(
        Path(__file__).resolve().parents[3] / "config" / "intervention.yaml")
    assert policy.decide("mcp_pdd_baiyi_mcp__get_baiyi_items", {},
                         default_level="confirm").level == "auto"
    # The exemption is per tool: another remote tool keeps the conservative default.
    assert policy.decide("mcp_other__read", {}, default_level="confirm").level == "confirm"
