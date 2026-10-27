import asyncio
from contextlib import asynccontextmanager
import logging

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentforge.models import ToolResult
from agentforge.redact import describe, mask_url
from agentforge.tools.base import bounded

logger = logging.getLogger(__name__)

# Hosted services cold-start slowly, and a call that is cut off leaves the
# remote side in an unknown state, so the default is generous rather than tight.
DEFAULT_TIMEOUT = 60.0


def headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@asynccontextmanager
async def connect(url: str, token: str | None = None, timeout: float | None = None):
    # trust_env stays off: a host-wide HTTP_PROXY breaks loopback and most
    # domestic endpoints, and the proxy would see the bearer token.
    async with httpx.AsyncClient(trust_env=False, timeout=timeout or DEFAULT_TIMEOUT,
                                 headers=headers(token)) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class MCPTool:
    source = "mcp"

    def __init__(self, url: str, definition, token: str | None = None,
                 timeout: float | None = None):
        self.url, self.name = url, definition.name
        self.remote_name = definition.name
        self.token, self.timeout = token, timeout or DEFAULT_TIMEOUT
        self.description = definition.description or definition.name
        self.schema = definition.inputSchema
        self.permission = "auto" if self.name in {"read_file", "grep", "glob"} else "confirm"

    async def execute(self, args: dict) -> ToolResult:
        try:
            async with asyncio.timeout(self.timeout):
                async with connect(self.url, self.token, self.timeout) as session:
                    result = await session.call_tool(self.remote_name, args)
            output = bounded("\n".join(c.text for c in result.content if c.type == "text"))
            structured = result.structuredContent
            if isinstance(structured, dict) and "ok" in structured:
                return ToolResult.model_validate(structured)
            if result.isError:
                logger.warning("mcp tool %s at %s returned an error", self.name, mask_url(self.url))
            return ToolResult(ok=not result.isError, output=output,
                              error="MCP tool failed. Inspect arguments and retry." if result.isError else None)
        except Exception as exc:
            # The remote side may have finished the work before the timeout, so
            # the message tells the caller to check state instead of retrying.
            logger.warning("mcp tool %s at %s failed: %s", self.name, mask_url(self.url),
                           describe(exc))
            return ToolResult(ok=False, error="MCP call failed or timed out. Check the external service; "
                "the remote operation may have completed, so verify its state before retrying.")


async def discover_tools(url: str, token: str | None = None,
                         timeout: float | None = None) -> list[MCPTool]:
    async with connect(url, token, timeout) as session:
        tools, cursor, seen = [], None, set()
        while True:
            definitions = await session.list_tools(cursor=cursor)
            tools.extend(MCPTool(url, definition, token, timeout) for definition in definitions.tools)
            if len(tools) > 64:
                raise ValueError("MCP server exceeds the 64-tool limit.")
            cursor = definitions.nextCursor
            if cursor is None:
                return tools
            if cursor in seen:
                raise ValueError("MCP server repeated a pagination cursor.")
            seen.add(cursor)
