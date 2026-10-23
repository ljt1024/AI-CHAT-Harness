import asyncio
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from agentforge.mcp_client import discover_tools


class MCPServerRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    url: str = Field(max_length=2000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment or parsed.query):
            raise ValueError("Use an HTTP(S) MCP URL without credentials, query or fragment.")
        return url


async def server_tools(server):
    async with asyncio.timeout(15):
        tools = await discover_tools(server["url"])
    if len(tools) > 64:
        raise ValueError("MCP server exceeds the 64-tool limit.")
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError("MCP server returned duplicate tool names.")
    for tool in tools:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,38}", tool.name):
            raise ValueError("MCP tool names must use 1..38 letters, digits, underscores or hyphens.")
        tool.remote_name = tool.name
        tool.name = f"mcp_{server['id']}__{tool.name}"
        tool.description = f"External MCP service ({server['id']}): {tool.description}"
        tool.permission = "confirm"
    return tools
