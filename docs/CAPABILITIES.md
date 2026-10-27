# Skills and MCP

The Agent mode chat runs on the Harness runtime, not the legacy Node backend. The current
local frontend is http://127.0.0.1:5196/ai.

The Skills & MCP panel and the Docker sandbox checkbox are hidden in the product UI
(`SHOW_SANDBOX_AND_SKILLS_UI` in `src/features/chat/constants/index.ts`). Hiding them changes no
behaviour: agent runs always request the Docker sandbox, and the agent still discovers and loads
installed skills on its own through `list_skills` / `load_skill`. Skills are installed, listed and
deleted through the agent (`install_skill` / `delete_skill`) or the REST API below. Start the
frontend with `VITE_SHOW_SANDBOX_UI=true npm run dev` to bring both controls back; the Playwright
verification scripts below require that flag, because they drive those controls.

## Response Language

Ordinary chat and Agent mode follow the latest user request's language. Explicit response
language requests and translation targets take priority. Chinese questions receive Chinese
explanations and answers even after English tool results, skill instructions or older replies.
Code, commands, paths and identifiers retain their original spelling. The policy is refreshed
on every Agent model call, including after tools, without accumulating in session history.
It is applied to the leading system prompt and the final per-call constraints. A Chinese task
sets a turn-specific Chinese default, including the first progress sentence before tools;
internal retry/compaction prompts cannot change that default. Explicit language overrides
still take priority. Checkpoint resume retains the original task's language context.

Trace turns record `model_messages` and `enabled_tools` alongside historical `messages`, so
the actual language policy sent to the model can be inspected. From `harness/`, run
`uv run python ../scripts/verify_response_language.py --history-session <session-id>` to test
a copy of an existing conversation with real model calls. The check covers English skill
instructions, streamed progress before tools, short HTML generation, explicit English replies
and translation; it does not modify the original conversation or approve file/command actions.

## Long Output Recovery

The DeepSeek adapter defaults to 8192 output tokens and preserves explicit limits. Truncated
responses or malformed tool arguments are rejected before any call in that response executes.
The agent can retry with smaller output at most twice, subject to the same run budget and turn
limit; previously successful tools are not replayed. Recovery appears in the execution trace.
Long code should use separate files or `write_file` chunks with explicit `append: true` for
later chunks. Writes replace by default; ordinary sandbox writes/appends use workspace rules.
This recovery does not automatically retry failed shell commands or other tool side effects.
Terminal failures have a specific error code and actionable message. See ADR-0011 and
`scripts/verify_html_generation.py` for the original HTML-task regression workflow.

`read_file` uses zero-based character offsets, not line numbers. Pages contain at most 1200
characters of file content, with an exact `next_offset` when truncated and EOF metadata when
continuation reaches the end. This stays within Docker JSON transport bounds, including
Chinese text, and prevents repeated reads of the wrong part of a long skill or generated file.

## Skills

New Agent mode conversations default to Docker sandbox. The agent has `list_skills` and
`load_skill` tools: it can discover installed skills and load a relevant skill without user
preselection. Loading returns SKILL.md and synchronizes files, but does not execute scripts.
Script commands run automatically inside the offline Docker sandbox unless a saved agent explicitly
requires confirmation. Automatically loaded skills appear as "Loaded by agent"
in the panel and remain available on subsequent turns.

Open Skills & MCP and choose a folder using Install skill folder. Submit a task; selecting
the installed entry is optional and preloads its instructions. An executable example
is at `examples/skills/csv-summary`: it includes SKILL.md, a Python script and a CSV sample.
Ask it to process the bundled sample. Expected output is 3 rows,
quantity total 15 and revenue total 78.00.

The server stores immutable source versions in MongoDB and synchronizes selected files into
`/workspace/.skills/<package-id>/` inside the session container each turn. SHA256 comparisons
skip unchanged files and restore missing or modified files after reconstruction.
The model receives the selected SKILL.md and actual directory. Scripts execute only through
run_command calls in Docker, never during import or on the host.

## Approval Policy

When confirmation is required, the pending operation replaces the bottom chat composer instead
of appearing above the conversation. Tool arguments scroll independently; approve/reject and
stop remain visible. Submission failures allow retry. Resolution, timeout or cancellation restores
the composer; resolved records remain in the current conversation view. Pending approval takes
priority over an open artifact preview and is scoped to the selected session.

Default Docker conversations execute workspace file operations and shell/installed-skill commands
automatically. The real security boundary is the managed offline container: read-only root, only
the session workspace mounted writable, no credentials or host mounts outside that workspace,
resource caps and command timeout. Arbitrary scripts can modify/delete files in their own sandbox;
this is not a promise that automatic commands are read-only. Outputs remain visible in the trace.

Host shell commands, skill installation/deletion and external MCP calls still require confirmation.
Saved-agent `permissions` can escalate either `run_command` or `write_file` to `confirm`.
Denied actions do not receive remembered or blanket approval, and unavailable Docker never falls
back to automatic host execution. Invalid/escaping file paths remain blocked, not auto-approved.

Existing host-tool conversations are not silently migrated. Selecting a skill or using
New skill conversation offers to create a sandbox conversation while preserving the old one.

Import supports up to 100 UTF-8 text files, 1 MB per file, 24 KB for SKILL.md and 5 MB total.
Folder names must start with an English letter. Binary assets are not supported yet.
deps.txt may be included but is not automatically installed. The current sandbox is offline
and contains Python's standard library; arbitrary third-party dependencies are not bundled.

GitHub imports spend only two API calls (commit lookup + recursive tree) regardless of file
count: file contents come from raw.githubusercontent.com, falling back to one codeload commit
archive when raw is unreachable. Set `GITHUB_TOKEN` (or `AGENTFORGE_GITHUB_TOKEN`) to raise the
API budget if a repository still reports rate limiting.

## Installing Through the Agent

The agent can call `install_skill` after human approval. For example, ask:
"Install https://github.com/anthropics/skills/tree/main/skills/frontend-design."
The agent should request a source when none is supplied, not invent a repository.
Successful installation returns the actual package ID, SHA256 and provenance, emits
`skill_installed`, and refreshes the chat catalog without a page reload.

Supported tool sources:
- `github`: `url` is a public HTTPS GitHub repository or tree-folder URL. A repository URL
  can also supply `path` and `revision`; branches containing slashes require explicit revision.
  A GitHub blob URL pointing to SKILL.md is also accepted. Downloads use the GitHub API,
  pin one commit across all files, reject redirects and never run repository code.
- `workspace`: `path` is a folder inside the current Docker /workspace. Source files are
  read via a bounded container archive, without following links or importing host directories.
- `inline`: `name` and `files` contain the skill contents explicitly supplied or requested
  by the user. This enables creating and installing a skill within one task.

All sources use the same package limits and validation as folder uploads. No download or
database write happens before approval. A rejected or invalid install creates no skill record.
Installation is independent of activation: use `load_skill` afterward to synchronize it.
GitHub and inline imports also work in host-tool conversations, but loading/running requires
Docker. Installation does not install pip/npm dependencies, binary resources, or private repos.
The downloader does not use ambient proxy variables; access to api.github.com must be available.

## Deleting a Skill

The agent can call `delete_skill` with an exact catalog ID after human approval. For example,
ask: "Delete the frontend-design skill." The tool removes the skill record, unloads it from the
current run, and deletes `/workspace/.skills/<id>` from this conversation's sandbox, refusing to
follow a symlinked skill directory. It never touches other workspace files, and it is refused
when a saved agent still selects the skill (remove it from that agent first). The same effect is
available in the Skills & MCP panel: Remove asks for confirmation, then calls `DELETE /api/skills/{id}`,
which also cleans synced copies in every managed workspace and emits `skill_deleted`.
Deletion is irreversible; re-install from the original source to restore a skill.

## MCP

Run the repository's MCP server from the Harness environment:

```sh
cd harness
MCP_WORKSPACE=../.agentforge/mcp-workspace uv run python ../mcp-server/src/main.py
```

In the panel, enter server ID `local` and URL `http://127.0.0.1:9000/mcp`, then connect.
Discovered names include `mcp_local__read_file` and `mcp_local__write_file`. Select the server,
ask the agent to use a specific MCP tool, then approve its call. This also works without
Docker mode because the selected MCP service executes externally. Its files are in its
own configured workspace, not the chat session's Docker workspace.

The running local development MCP process uses the command above. The service must be
restarted separately if stopped; stored registrations do not start remote processes.
This implementation supports Streamable HTTP, not stdio or legacy SSE.
All remote calls require approval, including reads. A timed-out remote operation can still
have completed, so inspect external state before retrying.

### Hosted services and pasted configs

A hosted deployment such as ModelScope hands out a ready-made block:

```json
{"mcpServers": {"fetch": {"type": "streamable_http",
  "url": "https://mcp.api-inference.modelscope.net/<tenant>/mcp",
  "headers": {"Authorization": "Bearer <ModelScope token>"}}}}
```

Paste it into **Skills & MCP → Paste an MCP config** and press **Import and connect**. The
config key (`fetch`) becomes the server ID, the bearer token is folded into the stored record,
and the server is selected for the next run. Only the `Authorization` header is honoured, and
re-pasting the same URL updates that row instead of adding a duplicate, so a rotated token
takes one paste. `POST /api/mcp/servers/import` with `{"config": "<the JSON text>"}` does the
same from the command line. One server per paste; several entries are refused with a message.

The token never leaves the backend: listings report `hasToken`, logs and error messages replace
the tenant path segment with `****`, and a 401/403 during discovery is reported as a rejected
token. Discovery times out after `MCP_DISCOVER_TIMEOUT` (30 s) and calls after
`MCP_CALL_TIMEOUT` (60 s); both are generous because hosted endpoints cold-start.

## API

- HTML deliverables: ordinary non-hidden workspace writes use path rules without per-file approval;
  explicitly configured `write_file: confirm` still requires approval. Docker commands also run
  automatically by default; host shell/MCP/install still require approval.
- Generated HTML is persisted and delivered via artifact events and `done.artifacts` with preview and
  download. Direct local CSS/JS/images are bundled; CSS imports/asset URLs and JS imports/fetch are not.
  Limit: 2 MB per bundled HTML, 20 entries per run. Preview remains script-free; open the downloaded
  file to run interactions. `GET /api/files/{id}/download` retrieves a saved delivery after restart.
  This endpoint is single-user only until planned authentication/ownership checks are implemented.

- `GET/POST /api/skills`: list/import `{name, files: {"SKILL.md": "...", ...}}`.
- `DELETE /api/skills/{id}`: uninstall; 404 when the skill is not installed, 409 when a saved
  agent still references it, and synced `.skills/<id>` directories are removed from workspaces.
- `GET/POST /api/mcp/servers`: list/register `{id, url, token?}` and discover tools.
- `POST /api/mcp/servers/import`: register from a pasted `{"mcpServers": {...}}` block;
  422 carries the reason the config cannot be used.
- `DELETE /api/mcp/servers/{id}`: remove; saved agent references prevent removal.
- `POST /api/agents/run`: accepts `skills`, `mcp_servers`, and `sandbox`.
- `POST /api/agents`: saved definitions also accept `skills` and `mcp_servers`.

The deployment is currently single-user and loopback-only. Skill import and MCP registration
are trusted administration operations. Do not expose these endpoints publicly without
authentication, authorization, endpoint allowlists and tenant isolation.

## Verification

The Playwright scripts drive the sandbox checkbox and the Skills & MCP panel, so run the
frontend with `VITE_SHOW_SANDBOX_UI=true npm run dev` before executing them.

```sh
cd harness
uv run pytest tests/unit tests/e2e -q
uv run --with playwright python ../scripts/verify_capabilities.py
uv run --with playwright python ../scripts/verify_capabilities.py --autonomous
uv run --with playwright python ../scripts/verify_skill_session.py
uv run --with playwright python ../scripts/verify_skill_installation.py
uv run --with playwright python ../scripts/verify_html_delivery.py --sandbox-checks
```

The browser script uses the actual configured model and running frontend/backend/MCP server.
It installs the CSV example, executes its script automatically, checks actual totals, then registers/selects
MCP, approves a write and verifies the external file. It also checks reload persistence and
desktop/mobile layouts. It creates local verification conversations and one external file.
Docker-dependent tests explicitly skip if Docker or the Python image is absent.
The autonomous variant asserts an empty request `skills` list and real `list_skills` /
`load_skill` calls. The skill-session regression uses a controlled SSE response only for
checking conversation routing; actual execution is covered by the real-model script.
The installation browser script tests actual GitHub approval and denial, provenance and live
catalog refresh. It removes only its uniquely named verification package after success.
