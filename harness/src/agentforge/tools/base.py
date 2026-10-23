from typing import Literal, Protocol

from jsonschema import ValidationError, validate

from agentforge.models import ToolResult


def bounded(text: str, limit: int = 4000) -> str:
    suffix = "\n[Truncated. Read a smaller range or narrow the search.]"
    if limit <= len(suffix):
        return text[:limit]
    return text if len(text) <= limit else text[:limit - len(suffix)] + suffix


class Tool(Protocol):
    name: str
    description: str
    schema: dict
    permission: Literal["auto", "rule", "confirm"]
    source: Literal["builtin", "mcp"]

    async def execute(self, args: dict) -> ToolResult: ...


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self.tools[tool.name] = tool

    def schemas(self, names: list[str]):
        missing = set(names) - self.tools.keys()
        if missing:
            raise ValueError(f"Unknown tools: {sorted(missing)}")
        return [{"type": "function", "function": {
            "name": self.tools[n].name, "description": self.tools[n].description,
            "parameters": self.tools[n].schema}} for n in names]

    async def execute(self, name: str, args: dict) -> ToolResult:
        if name not in self.tools:
            return ToolResult(ok=False, error="Unknown tool. Select a registered tool.")
        tool = self.tools[name]
        try:
            validate(args, tool.schema)
            result = await tool.execute(args)
            result.output = bounded(result.output)
            if result.error:
                result.error = bounded(result.error)
            return result
        except ValidationError:
            return ToolResult(ok=False, error="Invalid arguments. Check the tool's JSON schema.")
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=bounded(f"{exc}. Check workspace paths and retry."))
