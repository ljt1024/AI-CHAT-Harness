"""Product search over a Chinese affiliate catalogue, for price comparison.

The sandbox has no route to the internet (see ``web.py``), so this is another
harness capability: one GET to a fixed host, with a token from the environment.

Why a catalogue and not a scraper: every large Chinese marketplace serves its
search page behind a login, a slider or a signed request parameter. A crawler
that works today stops working next week, and the account behind it gets
throttled. The affiliate catalogues (淘宝联盟, 多多进宝, and the aggregators that
resell them) expose the same goods through a documented API with prices, coupon
amounts and sales. The cost is coverage: the pool holds the goods that carry a
commission, not the whole catalogue. Both the tool description and the output
say so, because a model that thinks it is looking at the entire market will
answer "this is the cheapest anywhere" from a partial list.

One provider is implemented today (淘客易, a REST wrapper with a bearer token).
The tools below only speak the normalised record, so another catalogue means
another mapping function, not another tool.
"""
from __future__ import annotations

import re

import httpx

from agentforge.config import Settings
from agentforge.models import ToolResult

BASE_URL = "https://www.tky.com/openapi/v1"
ITEM_PATH = "/goods"
DETAIL_PATH = "/goods/byitem"
TIMEOUT = httpx.Timeout(15.0, connect=6.0)
MAX_RESULTS = 20
DEFAULT_RESULTS = 8

# The catalogue's own sort keys, reached through words a model can choose from.
SORTS = {
    "relevance": "latest",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "sales": "daily_sales",
}

ITEM_ID = re.compile(r"[?&]id=(\d{6,})")


class EcomError(Exception):
    """A failure the model can act on, phrased as the next thing to try."""


def _token(settings: Settings) -> str:
    token = settings.taokeyi_token.get_secret_value().strip()
    if not token:
        raise EcomError(
            "No catalogue token is configured, so product search cannot run. "
            "The operator sets TAOKEYI_TOKEN in the environment; this is not "
            "something to ask the user for in chat. Until then, answer from "
            "what you already know and say the price is unverified.")
    return token


def _client() -> httpx.AsyncClient:
    # trust_env=False: a proxy configured for the service must not silently
    # become the path this request takes.
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT, trust_env=False,
                             headers={"Accept": "application/json"})


async def _fetch(client: httpx.AsyncClient, path: str, params: dict | None,
                 token: str) -> dict:
    """One request, with the catalogue's failures turned into sentences.

    Its error codes follow the HTTP status (401 token, 403 unverified or
    suspended, 429 quota, 503 upstream), so the status alone decides the advice.
    """
    try:
        response = await client.get(path, params=params,
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise EcomError(f"The catalogue could not be reached: {type(exc).__name__}.") from exc
    if response.status_code == 401:
        raise EcomError("The catalogue rejected the token. The operator needs to check "
                        "TAOKEYI_TOKEN.")
    if response.status_code == 403:
        raise EcomError("The catalogue account is not verified for API access, so no data "
                        "comes back. The operator has to finish the account verification.")
    if response.status_code == 429:
        raise EcomError("The catalogue's rate limit or daily quota is used up. Wait before "
                        "searching again rather than retrying in a loop.")
    if response.status_code == 404:
        raise EcomError("The catalogue has no such product. It may have delisted: a search "
                        "for the title finds whether a variant is still on sale.")
    if response.status_code >= 400:
        raise EcomError(f"The catalogue answered {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise EcomError("The catalogue returned something that is not JSON.") from exc
    code = payload.get("code")
    if code not in (0, None):
        raise EcomError(f"The catalogue refused the request: {payload.get('message') or code}.")
    return payload.get("data") or {}


def normalise(raw: dict) -> dict:
    """Map one catalogue record onto the shape the tools print."""
    return {
        "id": raw.get("itemid") or raw.get("goodsid"),
        "title": (raw.get("title") or "").strip(),
        # "price" is what the buyer pays after the coupon; the original is the
        # listing price, which is the only way to see whether a discount is real.
        "price": raw.get("price"),
        "original_price": raw.get("originalprice"),
        "coupon": raw.get("couponamount") or 0,
        "sales": raw.get("monthsales") or 0,
        "url": raw.get("itemlink") or "",
        "image": raw.get("mainimage") or "",
        "ends": raw.get("validuntil") or "",
    }


def _price_line(item: dict) -> str:
    price = item["price"]
    if price is None:
        return "   price not listed"
    line = f"   ¥{price}"
    was = item["original_price"]
    notes = []
    if was and was > price:
        notes.append(f"was ¥{was}")
    if item["coupon"]:
        notes.append(f"coupon ¥{item['coupon']}")
    return line + (f" ({', '.join(notes)})" if notes else "")


def format_items(keyword: str, items: list[dict]) -> str:
    if not items:
        return (f"No goods matched {keyword!r} in the affiliate catalogue. Try a shorter or "
                "more common term, or drop the price filter. This pool only holds goods that "
                "carry a commission, so an unusual item can be absent while still on sale.")
    blocks = []
    for index, item in enumerate(items, 1):
        stats = []
        if item["sales"]:
            stats.append(f"{item['sales']} sold/month")
        if item["ends"]:
            stats.append(f"offer ends {item['ends']}")
        lines = [f"{index}. {item['title']}", _price_line(item)]
        if stats:
            lines.append("   " + " | ".join(stats))
        if item["url"]:
            lines.append(f"   {item['url']}")
        blocks.append("\n".join(lines))
    blocks.append("These are catalogue quotes, cached up to 30 seconds, and the pool holds "
                  "commission-bearing listings only -- not every shop. For a price claim, "
                  "compare the same model across several rows and name the shop.")
    return "\n\n".join(blocks)


class ProductSearchTool:
    name = "search_products"
    description = ("Search Chinese e-commerce goods by keyword and get back titles, the price "
                   "after coupon, the original price, monthly sales and the listing link. Use "
                   "it when the user asks what something costs, which shop is cheaper, or for "
                   "a price range. The pool is an affiliate catalogue, not a full marketplace "
                   "index: popular goods are well covered, obscure ones may be missing, and a "
                   "missing result is not proof the item is unavailable. Prices are cached up "
                   "to 30 seconds. Do not claim you cannot look up prices: this tool reaches "
                   "the catalogue from outside the sandbox.")
    permission = "auto"
    source = "builtin"
    schema = {"type": "object",
              "properties": {
                  "keyword": {"type": "string", "minLength": 1, "maxLength": 60,
                              "description": ("What to look for, in Chinese: a product name "
                                              "or a model number. 'iPhone 16 Pro 256G' beats "
                                              "'phone'.")},
                  "min_price": {"type": "number", "minimum": 0,
                                "description": "Lowest acceptable price in yuan."},
                  "max_price": {"type": "number", "minimum": 0,
                                "description": "Highest acceptable price in yuan."},
                  "sort": {"type": "string", "enum": list(SORTS),
                           "description": ("relevance (default), price_asc, price_desc or "
                                           "sales. Use price_asc for 'cheapest'.")},
                  "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                            "description": "How many goods to return. Defaults to 8."}},
              "required": ["keyword"], "additionalProperties": False}

    def __init__(self, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None):
        self._settings = settings or Settings()
        self._client = client

    async def execute(self, args: dict) -> ToolResult:
        keyword = (args.get("keyword") or "").strip()
        if not keyword:
            return ToolResult(ok=False, error="A search needs a keyword.")
        min_price = args.get("min_price")
        max_price = args.get("max_price")
        if min_price is not None and max_price is not None and min_price > max_price:
            return ToolResult(ok=False, error="min_price is above max_price, which matches "
                                              "nothing. Swap or drop one of them.")
        params = {"keyword": keyword,
                  "sort": SORTS.get(str(args.get("sort") or "relevance"), "latest"),
                  "page": 1,
                  "size": min(int(args.get("limit") or DEFAULT_RESULTS), MAX_RESULTS)}
        if min_price is not None:
            params["minprice"] = min_price
        if max_price is not None:
            params["maxprice"] = max_price
        try:
            token = _token(self._settings)
            if self._client is not None:
                data = await _fetch(self._client, ITEM_PATH, params, token)
            else:
                async with _client() as client:
                    data = await _fetch(client, ITEM_PATH, params, token)
        except EcomError as exc:
            return ToolResult(ok=False, error=str(exc))
        items = [normalise(raw) for raw in data.get("items") or []]
        return ToolResult(ok=True, output=format_items(keyword, items), limit=6000)


class ProductDetailTool:
    name = "get_product"
    description = ("Look up one product's current catalogue record: price after coupon, "
                   "original price, sales and the listing link. Accepts a Taobao or Tmall "
                   "link or a numeric item id; ids that came from search_products work "
                   "directly. Use it to confirm one row before quoting a price, or when the "
                   "user pastes a link.")
    permission = "auto"
    source = "builtin"
    schema = {"type": "object",
              "properties": {
                  "item": {"type": "string", "minLength": 1, "maxLength": 500,
                           "description": ("A product id, or a link containing id=..." )}},
              "required": ["item"], "additionalProperties": False}

    def __init__(self, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None):
        self._settings = settings or Settings()
        self._client = client

    async def execute(self, args: dict) -> ToolResult:
        raw_item = (args.get("item") or "").strip()
        if not raw_item:
            return ToolResult(ok=False, error="A lookup needs an item id or link.")
        match = ITEM_ID.search(raw_item)
        item_id = match.group(1) if match else raw_item
        if "/" in item_id or " " in item_id:
            return ToolResult(ok=False, error=(
                "That looks like a link this tool cannot read an id from. Paste the item id "
                "or a link of the form item.taobao.com/item.htm?id=..."))
        try:
            token = _token(self._settings)
            path = f"{DETAIL_PATH}/{item_id}"
            if self._client is not None:
                data = await _fetch(self._client, path, None, token)
            else:
                async with _client() as client:
                    data = await _fetch(client, path, None, token)
        except EcomError as exc:
            return ToolResult(ok=False, error=str(exc))
        item = normalise(data)
        lines = [item["title"] or f"Product {item_id}", _price_line(item)]
        stats = []
        if item["sales"]:
            stats.append(f"{item['sales']} sold/month")
        if item["ends"]:
            stats.append(f"offer ends {item['ends']}")
        if stats:
            lines.append("   " + " | ".join(stats))
        if item["url"]:
            lines.append(f"   {item['url']}")
        return ToolResult(ok=True, output="\n".join(lines), limit=4000)


ECOM_TOOLS = (ProductSearchTool, ProductDetailTool)
# Resolved by name where an agent opts into a tool: an isolated run redirects
# every other name into the container, and these have to stay on the host.
ECOM_TOOL_MAP = {tool.name: tool for tool in ECOM_TOOLS}
