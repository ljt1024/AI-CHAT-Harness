# 0034 — The API is a package, and its boundaries are tests

Date: 2026-11-12
Status: Accepted

## Context

`harness/src/agentforge/api.py` was 1,873 lines: 46 routes, 16 request models,
two HTTP middlewares, the startup sequence, and **75 nested functions** inside
`create_app`. The routes were not the problem — their bodies were thin (median
16 lines, 853 lines in total). The problem was that every route closed over the
same fifteen locals (`settings`, `store`, `auth`, `pool`, `mail`, `sms`, `runs`,
`session_owners`, …), so no route could be moved without dragging the closure
with it. That is why the file had grown one feature at a time and why "the
interface layer is all in one file" was the accurate description.

Two smaller things kept the file sticky:

- `app.state.ready` is what the readiness checks read, and tests set it to
  `False` to simulate a database outage. Any split has to keep reading the app's
  own state rather than a captured boolean.
- Four tests import internals by path (`agentforge.api.LEGACY_EVENT_NAMES`,
  `agentforge.api.Runtime`, and a `SandboxPool` monkeypatch). A package has to
  keep those names reachable from the package root, or say plainly which one
  moved.

## Decision

1. **Split the module, not the logic.** `agentforge.api` becomes a package:
   `app` (construction), `deps` (`AppDeps`: the shared state plus the ~25 helper
   services built on it), `schemas`, `events`, `lifespan`, `middleware`, and
   `routes/` with one module per resource (auth, health, agents, runs, files,
   skills, mcp, sandbox, chat). Each router is `build_router(deps)`, and its
   first lines alias the few names it uses. Endpoint bodies moved verbatim: the
   indentation did not change, only `@app.post` became `@router.post`.
2. **The closure becomes an object.** `deps.__post_init__` builds the runtime
   and hands it `sync_skills` and `permission_check`; the runtime no longer knows
   about a closure. `deps.app` is kept so readiness still reads `app.state`.
3. **Prove the split changed nothing.** `app.openapi()` was captured before and
   compared after: 37 paths, 26 schemas, zero differences. Route order was kept
   (auth → health → agents → runs → files → skills → mcp → sandbox → chat) and
   middleware registration order was kept (authenticate, then log), because
   Starlette wraps in reverse and the logger must stay outermost or a 401 loses
   its `X-Request-Id`.
4. **Write the boundaries down as a test.** `tests/unit/test_layering.py` parses
   imports and fails if the API is imported from below it, if the shared leaf
   tier (`config`, `models`, `logging_config`, `redact`, `netguard`) reaches up,
   if `tools` imports `sandbox`/`loop`/`middleware`, or if two routers import
   each other. Comments cannot hold a boundary; a failing test can.
5. **Change one test path, deliberately.** `test_sandbox_exec` patched
   `agentforge.api.SandboxPool`; the pool is now constructed in `api.app`, so the
   patch target is `agentforge.api.app.SandboxPool`. Patching the package root
   would have looked fine and silently stopped replacing the pool.

## Consequences

A reader can now open `routes/auth.py` and see nine endpoints with no
construction in the way, and `deps.py` to learn what a handler is allowed to
know. `create_app` fits on a screen.

The rest of the tree is still flat: `auth.py` is 642 lines, and the domains that
belong together (`skills.py` + `skill_sources.py`, `mail`/`sms`/`phone`,
`artifacts`/`previews`/`session_files`) are still siblings by alphabet. Grouping
them is a second, riskier move — it rewrites imports everywhere — and is
deliberately not in this change. The layering test is what will make it safe
when someone wants it.

The package is one import target instead of one file, so `git log --follow`
across the split shows a rename plus edits rather than a clean history. That is
the price of the boundary; `git log --follow agentforge/api/routes/<name>.py`
still reaches the original lines.
