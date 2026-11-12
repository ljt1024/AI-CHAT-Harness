# User Authentication and Resource Ownership

## Decision

The Harness now authenticates every `/api` request with a server-side session cookie. This
implements T15.A1 and the storage/API half of T15.A2 from the task book, because the service
already runs on a public address while the plan still gates multi-user exposure behind M2A.

Identity comes from one place only: the `agentforge_session` cookie is resolved to a user by the
server and stored in a context variable that tool calls, background runs and approvals inherit.
No endpoint reads a user id from a body, a query string, a tool argument or a model message.

Passwords are hashed with `hashlib.scrypt` (N=16384, r=8, p=1, 16-byte salt, 32-byte digest) and
compared with `hmac.compare_digest`. The alternative was `argon2-cffi`; the standard library was
chosen because the deployment server builds its venv without a package proxy and an extra native
dependency would have to be provisioned twice (host and image). The encoded format carries its own
parameters, so moving to argon2 later is a rehash-on-login, not a migration.

Sessions are rows in `auth_sessions` holding the SHA-256 of the token, never the token itself.
Logout and revocation delete the row, so a stolen cookie dies server-side. Cookies are `HttpOnly`,
`SameSite=Lax`, `Path=/`, and `Secure` when `AGENTFORGE_COOKIE_SECURE` is on. `Lax` plus an
`Origin` check on unsafe methods covers CSRF without a token endpoint, which is why the frontend
needs no extra header plumbing.

Every stored resource carries `owner_id`: agents, sessions, traces, turns, tool_calls, checkpoints,
artifacts, skills, mcp_servers. Uniqueness moves from `id` alone to `(owner_id, id)`, which is what
lets two users install the same skill content or register a server with the same name. Reads,
deletes, resume, approval, SSE subscription and file download all filter by the caller's owner and
answer 404 rather than 403, so a wrong guess cannot confirm that someone else's id exists.

The `default` agent is shared and read-only: it has no owner, everyone may read and run it, and
`POST /api/agents` refuses that id so it cannot be captured by the first caller.

The first account is created explicitly from `AGENTFORGE_ADMIN_USER` / `AGENTFORGE_ADMIN_PASSWORD`
at startup, never by "first visitor wins". On the same startup, resources with no `owner_id` are
bound to that account (logged, idempotent, and skipped when an account already owns them). This is
the plan's "explicitly designate the initial user" step in its smallest workable form.

Not in this ADR: per-user workspace directories and sandbox leases (T15.A3), encrypted MCP
credentials (T15.A3), and the two-browser acceptance run (T15.A4). Session ids are UUIDs and every
entry point checks ownership, but workspaces still sit at `workspaces/<session_id>` until A3.

## Amendment: adopting legacy rows must skip the shared agent

Binding "resources with no `owner_id`" initially included the built-in agent. That is wrong, and it
is invisible to whoever gains it: the row simply becomes private to the first account, while every
other account — and every visitor trial — reads an empty catalogue and fails with
`Agent not found.` The delete-if-owned repair below is what shipped.

- `adopt_ownerless()` sets `owner_id` to the initial account except for `agents` rows carrying the
  reserved id; those it marks `owner_id: ""` (shared) rather than leaving them with no field, which
  matches nothing any account may read.
- Startup removes any private row under that reserved id before writing the shared one. No API path
  can create another row with this id (`POST /api/agents` refuses it), so such a row can only be
  this bug's leftover — and keeping it would show two agents with the same id to one account.

## Verification

`tests/unit/test_auth.py` covers hashing round-trips, wrong passwords, tampered encodings, expiry,
logout, revocation and cookie flags. `tests/e2e/test_user_isolation.py` drives two independent
clients against one app: anonymous calls to every protected route return 401, user B gets 404 for
A's session/agent/skill/trace/file/approval, same-content skill installs stay separate, and A's
runs and SSE stream are unaffected by B. `scripts/verify_auth_flow.py` exercises the same flow in
a real browser. The frontend login gate and 401 handling are checked with `npm run build`,
`npm run check:i18n` and the Playwright script.
