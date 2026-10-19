from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentforge.models import ToolResult
from agentforge.tools.base import bounded


@asynccontextmanager
async def connect(url: str):
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class MCPTool:
    source = "mcp"

    def __init__(self, url: str, definition):
        self.url, self.name = url, definition.name
        self.description = definition.description or definition.name
        self.schema = definition.inputSchema
        self.permission = "auto" if self.name in {"read_file", "grep", "glob"} else "confirm"

    async def execute(self, args: dict) -> ToolResult:
        async with connect(self.url) as session:
            result = await session.call_tool(self.name, args)
            output = bounded("\n".join(c.text for c in result.content if c.type == "text"))
            structured = result.structuredContent
            if isinstance(structured, dict) and "ok" in structured:
                return ToolResult.model_validate(structured)
            return ToolResult(ok=not result.isError, output=output,
                              error="MCP tool failed. Inspect arguments and retry." if result.isError else None)


async def discover_tools(url: str) -> list[MCPTool]:
    async with connect(url) as session:
        definitions = await session.list_tools()
        return [MCPTool(url, definition) for definition in definitions.tools]
