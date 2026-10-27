# AgentForge MCP server

Minimal MCP server used to exercise the harness client end to end: it exposes the
harness file tools over Streamable HTTP. The tool process receives an explicit
workspace and never receives model credentials.

It is a thin adapter over `agentforge`, so it declares that dependency instead of
relying on the caller's working directory to make the import resolve.

## Run standalone

```bash
cd mcp-server
uv run python src/main.py          # MCP_PORT, MCP_WORKSPACE
```

## Run from the harness environment

Useful when the harness venv already exists and a second one is not wanted:

```bash
cd harness
uv run python ../mcp-server/src/main.py
```

Either way the server binds loopback and reads its workspace from `MCP_WORKSPACE`
(default `.agentforge/mcp-workspace`), refusing paths outside it.
