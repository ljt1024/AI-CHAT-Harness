"""Run with the harness environment; bind to loopback and use an explicit workspace."""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agentforge.tools.fs import FileTool, Workspace
from agentforge.models import ToolResult

mcp = FastMCP("AgentForge tools", host="127.0.0.1", port=int(os.getenv("MCP_PORT", "9000")))
workspace = Workspace(Path(os.getenv("MCP_WORKSPACE", ".agentforge/mcp-workspace")))


async def execute(name, args):
    return await FileTool(workspace, name).execute(args)


@mcp.tool()
async def read_file(path: str, offset: int = 0, limit: int = 4000) -> ToolResult:
    """Read a UTF-8 file inside the assigned workspace."""
    if offset < 0 or not 1 <= limit <= 4000:
        return ToolResult(ok=False, error="Use offset >= 0 and limit 1..4000.")
    return await execute("read_file", {"path": path, "offset": offset, "limit": limit})


@mcp.tool()
async def write_file(path: str, content: str) -> ToolResult:
    """Write UTF-8 content inside the assigned workspace."""
    return await execute("write_file", {"path": path, "content": content})


@mcp.tool()
async def grep(query: str) -> ToolResult:
    """Find literal text in workspace files."""
    return await execute("grep", {"query": query})


@mcp.tool()
async def glob(pattern: str) -> ToolResult:
    """Find workspace file paths matching a glob."""
    return await execute("glob", {"pattern": pattern})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
