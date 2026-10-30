# AgentForge implementation

The two Chinese planning documents define the roadmap. Task book v3 is authoritative.
This repository imports AI-CHAT 2.1.1 at `fc78865fcea983503535f46f889886f33b28e872`.
The upstream `end/` directory is preserved as reference, including its dependencies.

## Prerequisites

- Node.js 22.13+, npm, Python 3.12+, uv, Docker Desktop/Engine.
- Configure local `.env` using the variable names in `.env.example`.
- Never commit `.env` or pass model credentials into tool processes.

## Infrastructure

```sh
docker compose up -d mongo
cd harness
uv run pytest tests/unit/test_config.py -q
```

## Base frontend

```sh
npm ci
npm run dev
npm run build
npm run check:i18n
```

The original backend can be started for contract comparison with
`npm ci --prefix end` and `npm run dev --prefix end`.

## Progress

### Update: 2026-10-20

Approval follow-up: offline Docker commands and installed skill scripts now execute automatically,
as do ordinary workspace writes. Host commands, installation and MCP still require confirmation;
saved agents with explicit confirm policies are unchanged. No host fallback or blanket permission
grant was added. 182 backend tests pass; see ADR-0013 for the Docker trust boundary and limitations.

Ordinary non-hidden sandbox writes now follow workspace path rules without per-file confirmation;
saved agents can still require confirmation explicitly. Host commands/install/MCP retain approval.
Generated HTML now reaches persisted artifact downloads and the existing frontend preview, with
direct local CSS/JS/images bundled (2 MB/HTML, 20 entries/run). Interactive scripts run only in the
downloaded file, not the sanitized preview. Real-model browser generation, download interaction,
reload and desktop/mobile checks passed, along with 175 backend tests. See ADR-0012 for limits.

MongoDB now starts successfully with `docker compose up -d --wait mongo`. If the configured
Docker Hub mirrors fail, use the official-image mirror and tag the images locally:

```sh
docker pull public.ecr.aws/docker/library/mongo:7
docker tag public.ecr.aws/docker/library/mongo:7 mongo:7
docker pull public.ecr.aws/docker/library/python:3.12-slim
docker tag public.ecr.aws/docker/library/python:3.12-slim python:3.12-slim
```

T10 runner and T11 pool are implemented and tested against real Docker. The runner strips
inherited image environment variables, runs as non-root, defaults to no network and destroys
the container on timeout/cancellation. Pool ownership isolates sessions and preserves the
workspace when rebuilding an expired container. The sub-500ms test measures warm acquisition.

T12 GridFS snapshot storage/restoration is implemented and tested against real MongoDB.
Restore requires an empty destination and rejects unsafe archive entries before writing.
The caller must stop writes and hold exclusive workspace ownership during snapshot/restore.

The production run factory now accepts Python sandbox policies and routes file and command
tools through Docker. Legacy chat requests can opt in with `sandbox: true`; session execution
mode cannot change after its first run. The pool starts maintenance during application startup
and releases leases after runs. The chat UI exposes sandbox selection and approval cards,
with session-scoped display and locally persisted environment selection.
Real Docker API tests cover approval-before-write, allow/deny outcomes, trace results and
absence of host-tool fallback. Generic file uploads, automatic snapshots, Node
image integration and browser sandbox management remain pending. The historical Tier 1
notes below describe the earlier checkpoint.

Run all available checks with `cd harness && uv run pytest tests/unit tests/e2e -q`.
Docker/MongoDB tests skip explicitly when their external prerequisites are absent.

- T0: Base import, runtime package, configuration, MongoDB compose, CI and ADR.
- T0 infrastructure acceptance is pending: local registries return EOF, malformed index, or referrers errors. Compose configuration validates, but MongoDB has not started.
- T0.5: HTTP/SSE moved into infrastructure; preserved sessionId/turnId; opt-in protocol stub.
- T1-T3: Gateway, MongoDB persistence/checkpoints, four restricted file tools; mock-backed tests pass.
- T4-T5: Real standalone MCP process and client, restricted commands with timeout and clean environment. Internet search/fetch tools remain unimplemented.
- T6-T9: Loop stop paths, concurrent-session rejection, checkpoint resume, compaction, seven middleware slots, approval API; tests pass.
- Sandbox/Skills middleware adapters are explicitly disabled until T10/T15. Builtin tools are the default runtime backend; MCP transport is independently usable and tested.
- No real model call was made: a DeepSeek API key has not been configured.
- This is a Tier 1 implementation checkpoint, not full T0-T20 acceptance.

## Run the runtime

Skills and MCP runtime integration is now available in the chat capabilities panel.
See [CAPABILITIES.md](CAPABILITIES.md) for supported import/transport limits, real execution
instructions and verification. Both workflows passed a browser check with the real model.

After MongoDB is available, configure `.env` locally using `.env.example` (never commit credentials):

```sh
cd harness
uv run uvicorn agentforge.server:app --host 127.0.0.1 --port 8000
```

In another terminal run `npm run dev`, then open `http://localhost:5174/ai`.
Use one Uvicorn worker: session locks, approval futures and SSE replay are in-process.
Without MongoDB the health endpoint reports `degraded`; without a model key runs return 503.
OpenAPI documentation is at `/docs`.

If port 8000 is occupied, run Harness on 8016 and launch Vite with:

```sh
AGENTFORGE_API_TARGET=http://127.0.0.1:8016 npm run dev -- --port 5186
```

Use the Vite proxy so API requests remain same-origin. The root route redirects to `/ai`.

## Verify

```sh
npm run build
npm run check:i18n
npm run lint
cd harness
uv run ruff check src tests
uv run pytest tests/unit tests/e2e/test_tier1_api.py -q
```

The API tests use mock MongoDB and a deterministic model fixture. MCP tests start a real
child server using an explicit environment with no model credentials. They are not Docker
or real-model acceptance tests. Frontend lint checks the migrated infrastructure and agent API;
the base's other TypeScript files retain their existing lint coverage.

## Protocol-only browser check

Start Harness using `AGENTFORGE_PROTOCOL_STUB=1` and Vite pointing to that server. This mode
only verifies the transport; it is opt-in and does not perform or simulate real agent work.

```sh
cd harness
AGENTFORGE_PROTOCOL_STUB=1 uv run uvicorn agentforge.server:app --host 127.0.0.1 --port 8016
# With the frontend already running, in another terminal:
uv run --with playwright python ../scripts/verify_harness_protocol.py --channel chrome
```

Omit `--channel chrome` when the Playwright Chromium browser is installed.
Snapshots go to ignored `out/browser/` and check desktop and mobile overflow.

## Scope and boundaries

- Tier 1 is for trusted, single-user, loopback development; it has no authentication.
- Authentication and private Skills/MCP are planned, not implemented: development plan section
  2.1 and task book T15.A1-A4 define the next platform security milestone (M2A). Multi-user
  exposure requires owner checks across APIs/tools/SSE/approvals/files, tenant sandbox boundaries,
  encrypted MCP secrets, explicit legacy-data migration and two-user acceptance, not just login UI.
- Tools use a dedicated per-session workspace, block credential paths and shell syntax,
  and pass only PATH/LANG/HOME to command processes. They are not a Docker security boundary.
- L3 requests suspend execution and must be resolved at `POST /api/approvals/{call_id}`;
  the frontend approval card is T14 work. Timeout always denies.
- Checkpoint resume is exposed through `Runtime.run(..., resume=True)`; workspace snapshot
  recovery, cross-process approval recovery, and browser resume are later tasks.
- The API supports creating/listing agents, native sessions/events, legacy agent runs,
  plain chat completions, approvals and traces. Full CRUD/files/skills/evals/Inspector
  and static production serving remain T12-T20 work.
- MCP must be launched with a sanitized environment and assigned workspace. Example:
  `env -i PATH="$PATH" MCP_WORKSPACE=/absolute/workspace uv run python ../mcp-server/src/main.py`.
