from contextlib import asynccontextmanager
import asyncio

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
        self.remote_name = definition.name
        self.description = definition.description or definition.name
        self.schema = definition.inputSchema
        self.permission = "auto" if self.name in {"read_file", "grep", "glob"} else "confirm"

    async def execute(self, args: dict) -> ToolResult:
        try:
            async with asyncio.timeout(30), connect(self.url) as session:
                result = await session.call_tool(self.remote_name, args)
                output = bounded("\n".join(c.text for c in result.content if c.type == "text"))
                structured = result.structuredContent
                if isinstance(structured, dict) and "ok" in structured:
                    return ToolResult.model_validate(structured)
                return ToolResult(ok=not result.isError, output=output,
                                  error="MCP tool failed. Inspect arguments and retry." if result.isError else None)
        except Exception:
            return ToolResult(ok=False, error="MCP call failed or timed out. Check the external service; "
                "the remote operation may have completed, so verify its state before retrying.")


async def discover_tools(url: str) -> list[MCPTool]:
    async with connect(url) as session:
        tools, cursor, seen = [], None, set()
        while True:
            definitions = await session.list_tools(cursor=cursor)
            tools.extend(MCPTool(url, definition) for definition in definitions.tools)
            if len(tools) > 64:
                raise ValueError("MCP server exceeds the 64-tool limit.")
            cursor = definitions.nextCursor
            if cursor is None:
                return tools
            if cursor in seen:
                raise ValueError("MCP server repeated a pagination cursor.")
            seen.add(cursor)
