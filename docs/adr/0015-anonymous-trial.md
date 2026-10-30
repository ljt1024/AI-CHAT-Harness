# ADR-0015: Anonymous trial — the login wall moves to the first turn that needs an account

Date: 2026-10-30
Status: Accepted
Builds on: ADR-0014 (accounts and ownership)

## Context

ADR-0014 put every `/api` request behind a session. That is the right default for
a multi-user deployment, but as a product experience it means a stranger who
opens the page sees a login form and nothing else — before they know what the
product does, they are asked to have an account. The development plan's own
product goal is that a visitor reaches the interface first and the account when
it earns its keep.

A trial must also not be a second open door. What it costs the operator is real
model calls, so the trial needs a budget, and the budget needs to survive a
cleared cookie.

## Decision

**A visitor without a session is not refused; it is given an identity.**

- The middleware mint a guest id (`guest_…`, random, shape-checked) and sets it
  in a 7-day `agentforge_guest` cookie. `current_user` — the single identity
  source every ownership filter already trusts — carries that id, so guest data
  is owned data, not ownerless data, and nothing downstream changes.
- Only a fixed set of routes is reachable through a guest: reading the agent and
  model catalogue, running a turn, and answering the approval that turn raises.
  Everything that mutates the installation — agents, skills, MCP servers,
  sandbox exec, deletes — is absent from that set and answers 401, which the
  interface turns into the sign-in dialog. A new endpoint stays protected by
  default exactly as in ADR-0014; the guest allowlist is explicit.
- The turn is charged *before* the model is called and handed back when refused.
  Two counters live in `guest_usage`, one row per visitor per day: the
  visitor's own (default 4) and a site-wide daily one (default 200), because a
  cleared cookie makes a new visitor. A refusal answers 403 with
  `detail: {code: "guest_limit", reason}` so the interface can open the right
  dialog instead of reporting a generic failure.
- **Signing in adopts the trial.** `login` reads the guest cookie and moves the
  guest's sessions, traces, turns, tool calls, checkpoints and artifacts to the
  account — the transcript the visitor was looking at survives sign-in instead
  of vanishing. Id collisions (a browser signed in here before) resolve in
  favour of the guest rows, which are the ones on screen.
- Unclaimed trials are deleted after `guest_retention_hours` (default 72), at
  startup. A guest that never chats leaves nothing to prune.
- `guest_turns=0` turns the trial off entirely and restores the ADR-0014 wall:
  every request 401s, `/api/auth/me` still answers so a login page can ask
  whether a session exists.

## Frontend

- `AuthGate` no longer holds the page hostage. Only the unknown first instant
  (`status === 'loading'`) is a placeholder; `anonymous` renders the product.
- The sign-in dialog is a layer, not a destination: it opens over whatever the
  visitor was doing, keeps the draft in the composer, and says *why* it opened —
  trial used up, daily budget spent, a step that needs an account, or an expired
  session (which now degrades to the dialog instead of discarding the page).
- The sidebar footer shows `访客试聊 · 剩 N 轮 / 登录` for a guest and the
  account with sign-out otherwise.

## Consequences

- The one identity source of ADR-0014 is unchanged; a guest is just an owner id
  that no account can name. There is still nothing to forge — an invented guest
  id is a fresh, empty visitor — but the cookie is shape-checked so it cannot
  become a malformed database key.
- The trial's leak surface is its budget: 4 turns of model spend per visitor per
  day, 200 site-wide, and no route that writes configuration. Operators on a
  public host should still set `ADMIN_PASSWORD`; the trial does not weaken
  fail-closed startup.
- Approval answers are permitted for guests because an interrupted turn that
  cannot be approved would strand the visitor mid-trial. Approvals resolve
  in-process and belong to the session that raised them, which is guest-owned.
- The old behaviour of "401 means show the login page" had to change everywhere
  at once; `test_user_isolation.py` now pins which routes are trial and which
  are account-only, and `test_guest_mode.py` pins the budget, adoption and
  pruning.
