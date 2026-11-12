# ADR-0026: A workspace belongs to the conversation, not to the process

Date: 2026-11-10
Status: Accepted
Related: ADR-0015 (anonymous trial), ADR-0016 (sandbox lease ownership), ADR-0020 (conversation file system)

## Context

Production reported a run that failed and kept failing on retry, shown as
`Run failed. Check configuration and retry.` The log had the reason in one line:

```
SandboxUnavailable: Session sandbox belongs to another account.
```

The reader had chatted as a visitor and then signed in with an email code. That
login did what it was designed to do — `adopt_guest` moved the session, its
turns, tool calls, checkpoints and artifacts from `guest_Xxabvw71…` to the new
account — but the sandbox lease kept the guest string, because it lives in the
pool's memory and nothing told it the reader had become an account. Every retry
hit the cross-account guard in `SandboxPool._reserve`, five times between
10:23:35 and 10:24:51. The guard is right: a session id is not an access right,
and a cached sandbox still holds the previous account's files. It was refusing
the one person who did own it.

The same session had a second failure waiting for the next restart. The map from
session to workspace is memory only, so a new process hands a conversation a
fresh directory; `gpu-price-report.html`, produced minutes earlier, becomes
unreachable and is eventually swept. Restarting to ship this fix would have
reproduced exactly that.

Both failures identify a workspace by something that changes: the owner string
at login, the process at start.

## Decision

Keep the identity where the workspace already lives — the conversation.

1. `SandboxPool.adopt_owner(previous, owner)` re-binds leases from the guest to
   the account, called from `finish_login` right after `adopt_guest`. It matches
   the exact previous owner, and that owner comes from the guest cookie of the
   browser asking to sign in, so this is not a way to claim somebody else's
   sandbox. Every other owner mismatch keeps the existing refusal.
2. The session → directory map is written to `sandboxes/sessions.json` and read
   back in `SandboxPool.start()`, *before* `sweep`, so the directories it names
   are protected instead of reclaimed. `acquire` persists after binding a
   session, `discard` persists after removing one.

Directory naming is untouched: still flat `sandboxes/<sandbox-id>`, including
directories mounted by warm containers. Renaming them per session would have
meant rebuilding a warm container at claim time — the one thing ADR-0016 keeps
from happening — so the map is persisted instead of the name being derived.

## Consequences

- Signing in no longer breaks a conversation that was running before it: same
  lease, same container, same files, the next turn continues.
- A restart no longer strands a conversation's outputs, which every deploy until
  now did silently. The map file is a cache of a path, not state: if it is
  missing or damaged the pool starts empty and a conversation gets a new
  directory, exactly as before.
- The map grows with the number of conversations that ever ran. Entries whose
  directory is gone are dropped on load, and `discard` removes them with the
  conversation.
- A deployment that swaps the whole `harness/` tree must keep
  `workspaces/sandboxes/` — the map points into it.
