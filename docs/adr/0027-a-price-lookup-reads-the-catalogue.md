# ADR-0027: A price lookup reads the catalogue, not the search page

Date: 2026-11-10
Status: Accepted
Related: ADR-0024 (web access is a harness capability)

## Context

The product question was "which shop is cheaper for this thing", and the obvious
implementation — scrape the Tsingtao/Taobao/JD/Pinduoduo search results — does
not survive contact with reality. Those pages need a login, a slider, or a
signed request parameter that changes. A scraper that works this week fails
next week, and the account whose cookie it borrowed gets throttled. The cost is
not the code; it is the steady stream of "why did it stop working".

The alternative is the affiliate catalogue: 淘宝联盟, 多多进宝, and the
aggregators that resell them behind a documented API. Same goods, same prices,
plus coupon amount and sales — and a contract that stays put.

## Decision

Two tools (`tools/ecommerce.py`) read a catalogue over one GET:

- `search_products` — keyword, price range, sort, returns title, price paid,
  original price, coupon, monthly sales, listing link;
- `get_product` — one record by item id or a Taobao/Tmall link.

The provider today is 淘客易 (bearer token). The tools only speak a normalised
record, so another catalogue is another mapping function, not another tool.

Three properties are deliberate:

1. **The coverage limit is stated, not hidden.** The pool holds commission-bearing
   listings, not the whole marketplace. Both the tool description and the output
   say so, because a model that believes it is looking at the whole market will
   answer "cheapest anywhere" from a partial list.
2. **A missing token is an explanation, not an exception.** The tool returns a
   sentence naming `TAOKEYI_TOKEN` and telling the model *not* to ask the user for
   it. An unconfigured deployment answers "price unverified" instead of inventing
   one, and the rest of the agent keeps working.
3. **Host, not container.** Like the web tools, these run in the harness: the
   sandbox has no route out and the token must never enter it
   (`ECOM_TOOL_MAP` branch in `api.py`).

`auto` permission: a read-only GET cannot change either side, and a dialog here
would only train the operator to click through approvals.

## Consequences

Anyone asking for a price gets catalogue prices, which lag up to 30 seconds and
do not cover shops that do not run an affiliate programme. For the common case —
"what does this cost, is this discount real" — that is enough, and it is honest
about what it is.

Comparing across platforms still needs a second provider (OneBound, or the
official 多多进宝 + 京东联盟 keys). That is a mapping function plus one settings
field, and the price of not doing it today is a narrower pool rather than a
wrong answer.

The `get_product` path takes an item id, not a search by link that requires
resolving a short link first; a short link without `id=` is refused with the URL
shape that does work.
