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

# A remote tool usually answers with one document -- a product list, a page of
# JSON -- so the local 4k ceiling would cut it mid-string and hand the model
# something it cannot parse. The ceiling is still a ceiling: 16k holds a 30-item
# listing with room to spare, and anything beyond that is a paging problem the
# tool's own arguments should solve.
OUTPUT_LIMIT = 16000


def headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def reason(exc, url: str, limit: int = 240) -> str:
    """The one line of a remote failure a caller can act on, endpoint masked.

    Swallowing this is expensive: "Browser not installed" is actionable, while
    "the call failed" leaves the model guessing and the run stuck.
    """
    text = describe(exc).replace(url, mask_url(url))
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:limit]


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
        # The service describes what its own tool does; a read that cannot destroy
        # anything does not need a human in the loop for every call. A server could
        # lie, which is why the trust boundary is "you registered this server" and
        # why `"mcp_*": confirm` in the policy overrides this for everyone at once.
        # A tool that declares nothing is not assumed safe, and its name is no
        # evidence: any server could call something `read_file`.
        annotations = getattr(definition, "annotations", None)
        self.read_only = bool(getattr(annotations, "readOnlyHint", False)) \
            and not bool(getattr(annotations, "destructiveHint", False))
        self.permission = "auto" if self.read_only else "confirm"

    async def execute(self, args: dict) -> ToolResult:
        try:
            async with asyncio.timeout(self.timeout):
                async with connect(self.url, self.token, self.timeout) as session:
                    result = await session.call_tool(self.remote_name, args)
            output = bounded("\n".join(c.text for c in result.content if c.type == "text"),
                             OUTPUT_LIMIT)
            structured = result.structuredContent
            if isinstance(structured, dict) and "ok" in structured:
                return ToolResult.model_validate(structured)
            if result.isError:
                logger.warning("mcp tool %s at %s returned an error: %s", self.name,
                               mask_url(self.url), output[:240])
                return ToolResult(ok=False, output=output,
                                  error=f"MCP tool reported an error: {output[:240]}",
                                  limit=OUTPUT_LIMIT)
            return ToolResult(ok=True, output=output, limit=OUTPUT_LIMIT)
        except Exception as exc:
            # The remote side may have finished the work before the timeout, so
            # the message tells the caller to check state instead of retrying.
            detail = reason(exc, self.url)
            logger.warning("mcp tool %s at %s failed: %s", self.name, mask_url(self.url), detail)
            return ToolResult(ok=False, error=f"MCP call failed: {detail}. Check the external "
                "service; the remote operation may have completed, so verify its state before retrying.")


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
