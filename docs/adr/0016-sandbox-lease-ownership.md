# Sandbox Leases Belong to an Account

## Decision

The sandbox pool now records which account holds a lease. This is the first half of T15.A3:
the container a conversation runs in is no longer reachable by naming its session id alone.

`Lease.owner` is set when a sandbox is claimed and survives `release()`, because a cached sandbox
is kept precisely so the same conversation can resume onto the files it wrote. A second account
that presents the same session id gets `SandboxUnavailable("Session sandbox belongs to another
account.")` instead of that conversation's workspace. The run path takes the owner from the run
lock (`session_owners`), which was recorded from the request's identity when the run started, so a
background task without the request's context cannot fall back to "whoever is running".

One account may hold at most `SANDBOX_MAX_PER_OWNER` (default 4) claimed sandboxes. Without a cap,
one user opening eight conversations takes the whole pool and every other account waits — the
shared-pool version of a noisy neighbour. `pool.discard()` also refuses a lease belonging to
someone else, so a caller that skipped its own ownership check cannot delete another account's
files by naming its session.

Workspace directories stay flat at `workspaces/sandboxes/<sandbox-id>`. Nesting them per account
(`<owner>/<sandbox-id>`) was the obvious layout, but a warm container is created *before* it knows
who will claim it, and its bind mount cannot be re-pointed at a moved directory — the container
would keep writing to an unlinked inode. Isolation therefore lives in the lease, not the path:
every session already gets its own directory, and the directory is only reachable through a lease
the owner check has approved.

`/health` stays public, so pool status reports an account as an 8-character SHA-256 prefix rather
than the raw `user_…` / `guest_…` id.

Still outstanding in T15.A3: encrypted MCP credentials, snapshot ownership, and the explicit
ownership migration with a dry run and a rollback rehearsal. T15.A4 (two-browser acceptance)
follows.

## Consequences

- A leaked or guessed session id is no longer enough to read another account's files.
- Per-account pool share is configurable; the default (4 of 8) leaves room for a second tenant.
- Warm sandboxes are unowned until claimed, so pre-warming still works; `status()` shows an empty
  digest for them.
- `pool.discard(session_id)` without `owner` keeps working (tests and callers that already checked
  ownership), but the API now always passes it.

## Verification

`tests/unit/test_pool.py` covers the four new rules: a cached sandbox refuses a different account
and still resumes for its own; a second account's claim is refused while others are unaffected;
status exposes only a digest; discard refuses another account's lease. The full backend suite
(`uv run pytest tests -q`, unit + real-Docker e2e) passes, and the run path was exercised through
`tests/e2e/test_user_isolation.py`, which drives two accounts against one app.
