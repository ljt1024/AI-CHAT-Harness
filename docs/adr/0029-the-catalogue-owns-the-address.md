# 0029: The catalogue owns the address

Status: Accepted

Context: Registration existed only as an administration surface behind
`VITE_SHOW_SANDBOX_UI=true`, documented as "single-user administration endpoints,
not a multi-tenant deployment" (ADR-0009). Offering it in the product raises the
question ADR-0009 deliberately deferred: an MCP registration makes the *host*
dial an address, and the host sits next to MongoDB, the model gateway and the
cloud metadata endpoint. A URL field in a page is therefore a request to use the
server's network position. Trusting the operator's file was fine; trusting every
account's typing is not.

Decisions:
- Add a catalogue: `mcp_catalog` rows (shared, empty owner, read-only for
  everyone) seeded from `config/mcp_catalog.json` at startup, carrying name,
  summary, category, URL, `auth` (`none` or per-account `token`), where to get
  that token, `verified`, and `private`.
- Install through `POST /api/mcp/catalog/{id}/install`. The body carries a
  credential and nothing else: the URL comes from the catalogue row, so the page
  that offers the feature has no URL to point anywhere. The resulting row is an
  ordinary `mcp_servers` row, so discovery, tool namespacing, masking, switching
  off, removal and run-time assembly are unchanged.
- Extract the address guard into `netguard` and share it with the web tools. The
  predicate stays `is_global` (loopback, RFC1918, link-local, CGNAT, reserved);
  registration refuses a non-global address unless `MCP_ALLOW_PRIVATE=true` or
  the catalogue entry is marked `private`. Link-local is refused even then,
  because no sidecar is addressed there. The startup file is the operator's own
  list and is trusted with private addresses; nothing a user types is.
- Record the classification on the row (`private`) instead of re-deriving it:
  a name can resolve somewhere else after it was registered, and the page needs
  to label what it is showing.
- Encrypt tokens at rest with Fernet (`enc:v1:` marker). The key is
  `MCP_SECRET_KEY` when set, otherwise a generated 0600 file beside the
  workspace. Rows written before this exist carry no marker and are read as
  plaintext, then rewritten once at startup. A key that no longer matches is
  reported, not treated as "no token".
- Keep manual entry: the same form and paste-import, folded away at the bottom of
  the page. It is the only place a URL is typed, and the only path that can be
  refused by the address guard on a user's behalf.

Consequences: A deployment with no catalogue file still has a working page — it
shows only what the account installed, plus manual entry. The catalogue is a seed,
so editing the file and restarting is how entries change; nothing is deleted for
being absent. Registration stays synchronous: installing a service has to prove
its credential, and an immediate tool list is what the 201 means. The page shows
the wait and the reason, and a retry is a second click rather than a background
state machine. Rotating `MCP_SECRET_KEY` invalidates existing tokens by design.
