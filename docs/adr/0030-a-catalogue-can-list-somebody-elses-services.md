# 0030: A catalogue can list somebody else's services

Status: Accepted

Context: ADR-0029 built the catalogue as an operator's private list: "whatever this
deployment standardises on". The first version shipped exactly one entry — the
bundled loopback demo — which makes the page look finished while offering nothing.
The product question behind it is different: a new account should be able to
subscribe to a service somebody else runs, the way every other tool market works,
without the deployment having negotiated anything.

Two facts constrain that. First, only Streamable HTTP is implemented
(`mcp_client.connect`); stdio needs a subprocess per account and SSE is the
transport the ecosystem is leaving. Second, the client sends a bearer header, so a
service that reads its key from a query parameter (`?key=`, `?ak=`) cannot be
reached at all — and those are exactly the map and search services a Chinese
deployment wants.

Decisions:
- **Ship a public list.** `config/mcp_catalog.json` carries thirteen public
  services in four categories (documentation and code, search and scraping, maps,
  company data) next to the demo row. A fresh deployment has a usable page
  without the operator writing a line, and an operator replaces or extends it by
  editing the file: entries are data, a broken one costs one row (ADR-0029).
  Every address in the file was answered by this repository's own client while
  writing it; `verified` means exactly that, not a quota or a support promise.
- **`{token}` marks where a credential goes.** An entry may write
  `https://mcp.amap.com/mcp?key={token}`; the account supplies the value and
  `endpoint()` substitutes it (percent-encoded) at the one place that dials out,
  `mcp_client.connect`. The stored row keeps the template, so the database holds
  one copy of the secret — the encrypted `token` column — and every account's row
  shows the same address. A literal query string is refused on both the catalogue
  and the registration validator: with no placeholder there is no way to tell a
  key from a parameter, and a key in a committed JSON file is a published key.
- **`auth: "optional"`.** Hosted trials (Exa, Firecrawl) answer without a key and
  rate-limit instead. Requiring a key would block a service that works; refusing
  the key would hide the upgrade path. The card says 「可选密钥」, install proceeds
  with no credential, and `endpoint()` drops the placeholder parameter rather than
  sending `?key=`, which reads as a key of the wrong shape and fails with a
  different message than "no key".
- **Name and summary carry an English copy.** Catalogue rows are operator data,
  not UI copy, so they cannot live in the locale files; `name_en`/`summary_en`
  fall back to the Chinese text instead of rendering an empty card.
- **What is left out, on purpose.** Services that only speak OAuth (Notion,
  Linear, Sentry, Feishu, DingTalk) and services that only speak SSE are not
  listed. A card that cannot be completed is worse than an absent card: the user
  would paste an API key into a flow that never accepts it. OAuth support would be
  a separate ADR, with a token store and a redirect handler this backend does not
  have.

Consequences:
- Installing is per account: the catalogue is shared read-only, the resulting
  `mcp_servers` row is owned by the installer, and two accounts can hold different
  keys for the same service. Removal and switching off are unchanged.
- A successful install does not prove the key works. Services validate lazily:
  Baidu answered a discovery with fourteen tools for a bogus `ak`, and only fails
  at call time with its own message. The page reports what happened rather than
  pretending a green tick means a working subscription; the model sees the
  service's error and can tell the user which key to renew.
- Keys in the query mean the URL is not a stable identifier for "same service" —
  `import_mcp_server` still compares URLs, so a re-import of a templated entry
  matches on the template, which is what it should match on.
- Each installed server costs its tool descriptions in every run's system
  message and the run refuses more than 128 tools (`api.py`). The page states the
  total it has enabled; a deployment offering twenty services should curate the
  list rather than let every account install all of them.
