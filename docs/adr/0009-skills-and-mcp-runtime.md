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
- Accept one optional bearer token per server. The token is stored with the registration and
  sent as an Authorization header; it never appears in a response, a log line or an error
  message, and hosted URLs have their opaque tenant path segment masked in the same places.
  Discovery and calls have separate generous timeouts, and an HTTP 401/403 during discovery
  reports a rejected token rather than a generic failure.
- Also accept the `{"mcpServers": {name: {...}}}` block that hosted catalogues hand out,
  verbatim: `type`, `url` and `headers.Authorization: Bearer ...` are folded into one stored
  token, and only that header is honoured so a config cannot smuggle others in. The catalogue
  key becomes the server id. Re-pasting the same URL refreshes that row instead of adding a
  duplicate, which is what happens after a token rotates.

Consequences: Registration is the opt-in. Because the product UI has no per-conversation
picker, a run with no pinned list attaches every registered server, while a pinned server that
is unregistered or unreachable fails the run and an auto-attached one that is offline is only
skipped. The panel stays a debug surface behind `VITE_SHOW_SANDBOX_UI=true`. These are local
single-user administration endpoints, not a multi-tenant deployment. stdio/SSE transports,
binary skill files and automatic dependencies remain pending.
