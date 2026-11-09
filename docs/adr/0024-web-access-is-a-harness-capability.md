# ADR-0024: Web access is a harness capability, not a container one

Date: 2026-11-09
Status: Accepted
Related: ADR-0006 (Docker sandbox), ADR-0013 (autonomous sandbox commands), ADR-0019 (retries)

## Context

A user asked the deployed assistant for a live sports result. It answered, correctly
and uselessly:

> 这个环境完全离线：容器网络不可达，DNS 解析失败，我也没有任何网页搜索或浏览器工具；
> 技能库里同样没有战绩查询技能。任何号称能查接口的做法都会失败，我不会编造成绩给你。

Nothing was broken. `sandbox/docker_runner.py` pins `network_mode: "none"`, the only
tool that could reach outside was a remote MCP server, and the `run_command`
description said, in as many words, *"This cannot access the host filesystem or
network… report checks you cannot perform."* The model read its own capability
manifest and refused. The product promised an assistant and shipped an interpreter
locked in a room.

Two things were true at once and had been conflated:

1. **The container must stay offline.** It runs model-written code. Giving that code
   a socket is a different trust problem from fetching a page the model chose, and
   it would dissolve the isolation model the whole sandbox ADR is built on.
2. **The assistant needs the internet.** "I cannot look that up" is not an answer a
   user accepts twice.

## Decision

**Internet access is a capability of the harness process, exposed as two tools:
`web_search` and `web_fetch`. The sandbox keeps `network_mode: "none"`.**

- Both are registered in the built-in catalogue and in the default agent, and they
  are the only tools that survive an isolated run without being redirected into the
  container (`api.py`, the `WEB_TOOL_MAP` branch next to `ask_user`). That branch is
  the important one: without it an isolated agent would "have" `web_fetch` and get a
  DNS failure dressed up as a tool result.
- `permission: auto` for both. A confirm dialog on a read-only GET would train users
  to click through approvals, which is how the gates that matter get ignored.
- `run_command`'s description was rewritten to point at these tools instead of
  telling the model to report what it cannot do. **A tool description is a policy
  document**: the old wording is what produced the paragraph above.

### What the two guards are for

`_assert_reachable` resolves every hop — the URL *and* each redirect — and rejects
anything that is not a global unicast address. `ipaddress.is_global` is the single
predicate covering loopback, RFC1918, link-local (including the cloud metadata
endpoint at 169.254.169.254), CGNAT and reserved space, so there is no denylist to
fall behind. Redirects are followed by hand for the same reason: `follow_redirects`
would re-check nothing after the first hop.

`http`/`https` only, no userinfo in the URL (a credential cannot be smuggled into
the authority), `trust_env=False` (an HTTP proxy configured for the service must not
become the path these requests take, or the guard above stops meaning anything),
2 MB cap enforced while streaming, 15 s timeout, text content types only.

### Why two search engines

Measured from the production host, on the same query (`国足 世预赛 最新战绩`):

| Engine | Result |
| --- | --- |
| Bing CN (HTML or RSS) | returns encyclopedia entries about 中国 — plausible, wrong |
| Bing CN (in a real browser) | same, so it is the index, not the request |
| 360 (`so.com`) | 虎扑 / 光明网 / 积分榜, on topic |
| Sogou | on topic, then a captcha after ~6 requests from this IP |

So the query's language picks the first engine (CJK → 360, otherwise → Bing RSS) and
the other is the fallback. 360 keeps real targets in `data-mdurl`, so no click-counter
resolution is needed. Bing is read as RSS (`format=rss`): same index, 3 KB of XML
instead of 100 KB of HTML, real links.

Three consequences were accepted rather than hidden:

- **Relevance is checked, not assumed.** `relevant()` keeps only rows sharing a term
  with the query. This is what turns Bing's "answer about 中国" into a fallback instead
  of a confident wrong answer. It can drop a legitimate result that phrases things
  differently; that is the cheaper error.
- **Rate limiting is real.** One second between 360 calls, plus a 5-minute in-process
  cache, so a burst of users does not become a captcha page for everyone.
- **Scraping is a stopgap.** A layout change or a captcha breaks search, not the
  product: `web_fetch` still works on any URL the model is given, and the failure is
  reported as a failure. The upgrade path is a search API key (博查 / Tavily / Serper)
  behind the same tool — the parsing functions are pure and already testable.

## Consequences

- The sandbox is still `network_mode: "none"`. `run_command` still cannot `curl`. The
  isolation boundary is unchanged; what changed is that the *harness* has a narrow,
  audited egress path that the model can call.
- `web_fetch` can carry workspace content out in a query string. Any egress path has
  that property; here the model has to be told to do it and the trace shows the URL.
  If that becomes unacceptable, the change is one policy rule, not a rewrite.
- Existing agents do not gain the tools automatically: an agent's tool list is data.
  The wizard's defaults include them from now on, and the built-in `default` agent is
  re-seeded on every boot.
- Search results are third-party content and can be wrong, stale or adversarial. The
  tool tells the model to read the page before quoting a number; the approval model
  does not extend to believing what a page says.
