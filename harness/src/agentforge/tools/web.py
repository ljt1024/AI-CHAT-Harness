"""Reading the open web, from the host process and nowhere else.

The sandbox is network-isolated on purpose (``network_mode: "none"``): it runs
model-written code, and handing that code an unrestricted socket is a different
trust problem from fetching a page the model asked for. So internet access is a
capability of the harness, exposed as two narrow tools that can only issue an
HTTP GET to a public address -- no ports, no credentials, no shell.

Two guards keep "public address" true rather than assumed:

* every hop (the URL and each redirect) is resolved and rejected unless the
  address classifies as global, which excludes loopback, private, link-local,
  shared and reserved ranges in one test;
* the transport is http/https with no userinfo, so a URL cannot smuggle a
  credential or speak to a unix socket.

What this does not stop: a public URL that carries workspace content in its
query string. Any egress path has that property; the mitigation is that the
agent has to be told to do it, and the trace shows the URL.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

import httpx

from agentforge.models import ToolResult

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT,
           "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Two engines, because neither is sufficient alone: Bing CN returns unrelated
# results for some Chinese news queries (checked against a real browser, so it is
# the index, not the request), while 360 answers those and starts demanding a
# captcha if asked too fast. So the query language picks the first engine and the
# other one is the fallback -- a failure in one is not a failure of the tool.
SO_URL = "https://www.so.com/s"
BING_RSS = "https://cn.bing.com/search"
CJK = re.compile(r"[\u3400-\u9fff]")
MAX_RESULTS = 10
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
FETCH_TIMEOUT = httpx.Timeout(15.0, connect=6.0)
# 360 meters datacenter addresses by request rate; one second between calls and a
# short cache keep a burst of users from turning into a captcha page.
SO_MIN_INTERVAL = 1.0
CACHE_TTL = 300
CACHE_SIZE = 64
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml",
              "application/rss+xml", "application/atom+xml")


class WebError(Exception):
    """A failure the model can act on, phrased as the next thing to try."""


def _assert_public(url: str) -> str:
    """Return the URL unchanged, or raise with the reason it is not fetchable."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise WebError(f"Only http and https URLs can be fetched, not {parts.scheme or 'a relative path'}.")
    if parts.username or parts.password:
        raise WebError("Remove the credentials from the URL; this tool does not send them.")
    if not parts.hostname:
        raise WebError("That URL has no host name.")
    return url


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebError(f"DNS lookup failed for {host}: {exc.strerror or exc}.") from exc
    return sorted({entry[4][0] for entry in info})


async def _assert_reachable(url: str) -> None:
    """Reject anything that is not a global unicast address.

    ``is_global`` is the single predicate that covers loopback, RFC1918,
    link-local (including the 169.254.169.254 metadata endpoint), CGNAT and
    reserved space, so there is no denylist here to fall behind.
    """
    _assert_public(url)
    host = urlsplit(url).hostname or ""
    for address in await _resolve(host):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise WebError(f"{host} resolved to an address this tool cannot classify.") from exc
        if not ip.is_global:
            raise WebError(f"{host} resolves to {address}, which is not a public address.")


async def _get(url: str) -> tuple[str, str, bytes]:
    """GET with the guards applied to the first URL and to every redirect.

    Returns the final URL, the content type and the body. A redirect is followed
    by hand rather than by httpx, because the address check has to run again on
    each hop or a public URL can bounce the request into a private one.
    """
    current = url
    # trust_env=False: a proxy configured for the service must not silently become
    # the path these requests take, or the address check above stops meaning anything.
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False,
                                 headers=HEADERS, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _assert_reachable(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise WebError(f"{current} redirected without a target.")
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code >= 400:
                    raise WebError(f"The server answered {response.status_code} for {current}.")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise WebError(f"That page is larger than {MAX_BYTES // 1000} KB.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise WebError(f"That page passed {MAX_BYTES // 1000} KB mid-download; "
                                       "try a more specific URL.")
                    chunks.append(chunk)
                return current, response.headers.get("content-type", ""), b"".join(chunks)
    raise WebError(f"{url} redirected more than {MAX_REDIRECTS} times.")


class _TextExtractor(HTMLParser):
    """HTML to readable text without a parser dependency.

    Everything that renders as nothing in a browser -- scripts, styles, hidden
    templates -- is dropped by tag, and block level elements become newlines so
    sentences from different cells do not run together.
    """
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
             "h6", "section", "article", "header", "footer", "blockquote", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\u00a0", " ")
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.title.strip(), parser.text()


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    candidates = [match.group(1)] if match else []
    candidates += ["utf-8", "gb18030"]
    for charset in candidates:
        try:
            return body.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _is_text(content_type: str) -> bool:
    value = (content_type or "").split(";")[0].strip().lower()
    return any(value.startswith(kind) or value.endswith("+json") or value.endswith("+xml")
               for kind in TEXT_TYPES)


def _text(fragment: str) -> str:
    """Strip tags and collapse the whitespace the engine leaves in highlights."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_so(markup: str, limit: int) -> list[dict]:
    """Read 360's result list.

    360 keeps the real target in ``data-mdurl`` and only uses ``href`` for its own
    click counter, so the attribute is the address worth reporting; resolving the
    counter link costs one request per result and returns a script stub.
    """
    blocks = re.findall(r'<li class="res-list.*?(?=<li class="res-list|</ul>)', markup, re.S)
    results = []
    for block in blocks:
        heading = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not heading:
            continue  # an AI card or an ad slot, neither of which has a target
        anchor = re.search(r"<a\b([^>]*)>(.*?)</a>", heading.group(1), re.S)
        if not anchor:
            continue
        attributes, title = anchor.group(1), _text(anchor.group(2))
        target = re.search(r'data-mdurl="([^"]+)"', attributes) or \
            re.search(r'href="([^"]+)"', attributes)
        if not target or not title:
            continue
        url = unescape(target.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        summary = re.search(r'<span class="res-list-summary"[^>]*>(.*?)</span>', block, re.S) or \
            re.search(r'<p class="res-desc"[^>]*>(.*?)</p>', block, re.S) or \
            re.search(r'<div class="res-desc"[^>]*>(.*?)</div>', block, re.S)
        results.append({"title": title, "url": url,
                        "snippet": _text(summary.group(1)) if summary else ""})
        if len(results) >= limit:
            break
    return results


def parse_bing_rss(markup: str, limit: int) -> list[dict]:
    """Read Bing's RSS feed: the same index as its HTML page in 3 KB of XML."""
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return []
    results = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if title and url.startswith(("http://", "https://")):
            results.append({"title": title, "url": url,
                            "snippet": _text(item.findtext("description") or "")})
    return results


def relevant(query: str, results: list[dict]) -> list[dict]:
    """Drop results that share no term with the query.

    This exists because an engine can answer the wrong question: Bing CN returns
    encyclopedia entries about "中国" for some Chinese news queries, and those
    look like real results. Keeping only the rows that mention at least one query
    term turns a confidently wrong answer into a fallback or an honest "not
    found". If nothing matches, the caller treats the engine as empty.
    """
    tokens = [token for token in re.split(r"[\s，,。、;；:：()（）\"'“”]+", query)
              if len(token) >= 2]
    if not tokens:
        return results
    kept = [item for item in results
            if any(token.lower() in f"{item['title']} {item['snippet']}".lower()
                   for token in tokens)]
    return kept


def format_results(query: str, results: list[dict]) -> str:
    if not results:
        return (f"No results for {query!r}. Say you could not find it rather than "
                "guessing; a different phrasing sometimes helps.")
    lines = [f"{index}. {item['title']}\n   {item['url']}\n   {item['snippet']}"
             for index, item in enumerate(results, 1)]
    return "\n".join(lines) + ("\n\nThese are snippets. Read a page with web_fetch "
                               "before relying on its numbers or dates.")


_cache: OrderedDict[tuple[str, int], tuple[float, list[dict]]] = OrderedDict()
_so_lock = asyncio.Lock()
_so_last_call = 0.0


def _cached(query: str, limit: int) -> list[dict] | None:
    entry = _cache.get((query, limit))
    if entry and time.monotonic() - entry[0] < CACHE_TTL:
        _cache.move_to_end((query, limit))
        return entry[1]
    return None


def _remember(query: str, limit: int, results: list[dict]) -> None:
    _cache[(query, limit)] = (time.monotonic(), results)
    _cache.move_to_end((query, limit))
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


async def _so(query: str) -> list[dict]:
    """One 360 request, spaced out so a burst does not become a captcha page."""
    global _so_last_call
    async with _so_lock:
        wait = SO_MIN_INTERVAL - (time.monotonic() - _so_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            _, content_type, body = await _get(f"{SO_URL}?q={quote(query)}")
        finally:
            _so_last_call = time.monotonic()
    return parse_so(_decode(body, content_type), MAX_RESULTS)


async def _bing(query: str) -> list[dict]:
    _, content_type, body = await _get(
        f"{BING_RSS}?format=rss&q={quote(query)}&count={MAX_RESULTS}")
    return parse_bing_rss(_decode(body, content_type), MAX_RESULTS)


async def search(query: str, limit: int) -> list[dict]:
    """Ask the engine that suits the query, then the other one if it comes back empty."""
    cached = _cached(query, limit)
    if cached is not None:
        return cached[:limit]
    engines = (_so, _bing) if CJK.search(query) else (_bing, _so)
    errors = []
    for engine in engines:
        try:
            results = await engine(query)
        except (WebError, httpx.HTTPError) as exc:
            errors.append(str(exc))
            continue
        results = relevant(query, results)
        if results:
            _remember(query, limit, results)
            return results[:limit]
    if errors:
        raise WebError("Search failed: " + "; ".join(errors))
    return []


class WebSearchTool:
    name = "web_search"
    description = ("Search the public web and get back titles, URLs and snippets. Use it "
                   "for anything that changes over time or that you do not already know: "
                   "scores, prices, releases, opening hours, who currently holds a role. "
                   "Snippets are often stale or truncated -- follow up with web_fetch on "
                   "the most promising URL before stating a fact. Do not claim you cannot "
                   "search: this tool reaches the internet from outside the sandbox.")
    permission = "auto"
    source = "builtin"
    schema = {"type": "object",
              "properties": {
                  "query": {"type": "string", "minLength": 1, "maxLength": 300,
                            "description": ("What to look for, in the language the answer "
                                            "is likely written in. Specific nouns beat "
                                            "sentences: '国足 世预赛 战绩 2026'.")},
                  "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                            "description": "How many results to return. Defaults to 6."}},
              "required": ["query"], "additionalProperties": False}

    async def execute(self, args: dict) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="A search needs a query.")
        limit = min(int(args.get("limit") or 6), MAX_RESULTS)
        try:
            results = await search(query, limit)
        except WebError as exc:
            return ToolResult(ok=False, error=str(exc))
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"The search request failed: {type(exc).__name__}.")
        return ToolResult(ok=True, output=format_results(query, results), limit=6000)


class WebFetchTool:
    name = "web_fetch"
    description = ("Fetch one public web page and return its readable text, with scripts and "
                   "markup removed. Use it after web_search to confirm a number, a date or a "
                   "quotation, or when the user gives you a URL. It sends a plain GET: pages "
                   "behind a login, a paywall or a JavaScript-only shell return nothing "
                   "useful, and private or local addresses are refused by design.")
    permission = "auto"
    source = "builtin"
    schema = {"type": "object",
              "properties": {
                  "url": {"type": "string", "minLength": 8, "maxLength": 2000,
                          "description": "An absolute http(s) URL."}},
              "required": ["url"], "additionalProperties": False}

    async def execute(self, args: dict) -> ToolResult:
        requested = (args.get("url") or "").strip()
        if not requested:
            return ToolResult(ok=False, error="A fetch needs a URL.")
        try:
            final, content_type, body = await _get(requested)
        except WebError as exc:
            return ToolResult(ok=False, error=str(exc))
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"The request failed: {type(exc).__name__}.")
        if not _is_text(content_type):
            return ToolResult(ok=False, error=(
                f"That URL is {content_type.split(';')[0] or 'not text'}, not a page this tool "
                "can read. Give the user the link instead."))
        text = _decode(body, content_type)
        title, readable = (html_to_text(text) if "html" in content_type.lower()
                           else ("", text.strip()))
        if not readable:
            return ToolResult(ok=False, error=(
                "That page returned no readable text; it is probably built by JavaScript. "
                "Use the snippet from search or give the user the link."))
        header = f"URL: {unquote(final)}"
        if title:
            header += f"\nTitle: {title}"
        return ToolResult(ok=True, output=f"{header}\n\n{readable}", limit=8000)


WEB_TOOLS = (WebSearchTool, WebFetchTool)
# Looked up by name when an agent opts into a tool: an isolated run resolves every
# other name to a container command, and these two must stay on the host.
WEB_TOOL_MAP = {tool.name: tool for tool in WEB_TOOLS}
