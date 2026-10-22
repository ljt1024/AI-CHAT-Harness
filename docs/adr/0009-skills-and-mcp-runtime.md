# 0009: Connect Skills and MCP to the runtime and chat

Status: Accepted

Context: The MCP client was tested independently, but no runtime registry used it.
Skills were rejected by the agent API. Neither feature was usable from chat.

Decisions:
- Import UTF-8 skill folders as bounded JSON file maps, requiring SKILL.md.
- Persist immutable content-addressed package versions in MongoDB. Never execute imports.
- Require a Python Docker sandbox for skills. Inject selected instructions into the system
  prompt and use the SkillsSync middleware for SHA256 incremental file sync each turn.
- Recreate missing or changed files after sandbox reconstruction. Reject symlink destinations.
- Leave dependency installation and script execution to explicitly approved sandbox commands.
- Register MCP servers explicitly over Streamable HTTP. Discover tools at registration and
  again before runs, handle pagination, and namespace names as mcp_<server>__<tool>.
- Treat all MCP calls as external operations requiring approval, including read-like names.
  The remote service owns its workspace and isolation; the local Docker policy does not apply.
- Fail on discovery errors without builtin fallback. Bound call duration and surface unknown
  remote outcomes without automatic retries. No credentials are accepted in server URLs.

Consequences: The chat has actual import, connection, selection and removal controls.
These are local single-user administration endpoints, not a multi-tenant deployment.
Auth headers, stdio/SSE transports, binary skill files and automatic dependencies remain pending.
