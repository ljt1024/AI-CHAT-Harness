"""The service catalogue a deployment offers its users.

The point of a catalogue is not the list -- it is who typed the URL. Registering
an MCP server makes the *host* dial an address, so a user-supplied string is a
request to use the host's network position, while a catalogue entry is an address
the operator already chose. Installing from here therefore cannot be turned into
a probe of 127.0.0.1 or the metadata endpoint, and the user never has to know
what a Streamable HTTP endpoint looks like.

Entries are data, so a broken one is logged and skipped rather than taking the
whole file down. The file that ships with the repository lists public services --
whose addresses are published by their operators anyway -- and a deployment adds
whatever it standardises on: an internal gateway, a vendor's hosted server, one
sidecar per team.

A credential never appears in an entry. Where a service takes its key as a query
parameter, the URL carries `{token}` and the account supplies the value, which is
stored encrypted beside the row.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from urllib.parse import urlsplit

from agentforge.mcp_client import TOKEN_PLACEHOLDER

logger = logging.getLogger(__name__)

MAX_ITEMS = 64
MAX_FILE = 200_000


class CatalogItem(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=300)
    # Operator data rather than UI copy, so both languages travel in the file.
    # English falls back to the Chinese text instead of an empty card.
    name_en: str = Field(default="", max_length=80)
    summary_en: str = Field(default="", max_length=300)
    category: str = Field(default="general", max_length=40)
    url: str = Field(max_length=2000)
    # "none" is a server the deployment runs for everyone; "token" means each
    # account brings its own credential, which stays in that account's row;
    # "optional" is a hosted trial that answers without a key and gets better
    # with one, so the card must not block the install on a key.
    auth: Literal["none", "token", "optional"] = "token"
    # Where a user gets that credential. A dead end without it: the catalogue is
    # the only place the product can answer "what am I supposed to paste here".
    docs: str = Field(default="", max_length=500)
    verified: bool = False
    enabled: bool = True
    # Set by the operator for a sidecar on loopback, and the only reason
    # registration is allowed to dial a private address.
    private: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("Use an HTTP(S) MCP URL without credentials or fragment.")
        if TOKEN_PLACEHOLDER in parsed.netloc:
            raise ValueError("Put the token in the path or query, not the host name.")
        # Three hosted services in a row take the key as `?key=`/`?ak=`, so a
        # catalogue entry has to describe that. The placeholder keeps the value
        # out of this file: the row the user installs holds their own key in the
        # encrypted `token` column, and the URL stays identical for everyone.
        if parsed.query and TOKEN_PLACEHOLDER not in parsed.query:
            raise ValueError("A key in the URL would be stored in the clear. Write "
                             "https://host/mcp?key={token} and let the account supply it.")
        return url


class InstallRequest(BaseModel):
    """What an install may carry: a credential, never an address."""

    token: str | None = Field(default=None, max_length=512, repr=False)


def parse_catalog(path: Path) -> list[CatalogItem]:
    """Read the file, keeping the entries that are usable.

    A catalogue is operator configuration, edited by hand. One typo should cost
    one row, not the startup of the service, so every failure is a log line.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("mcp catalogue unreadable path=%s error=%s", path, exc)
        return []
    if len(text) > MAX_FILE:
        logger.warning("mcp catalogue ignored path=%s reason=too large", path)
        return []
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("mcp catalogue is not valid JSON path=%s error=%s", path, exc.msg)
        return []
    entries = document.get("servers") if isinstance(document, dict) else None
    if not isinstance(entries, dict):
        logger.warning("mcp catalogue needs a `servers` object path=%s", path)
        return []
    items: dict[str, CatalogItem] = {}
    for key, spec in list(entries.items())[:MAX_ITEMS]:
        if not isinstance(spec, dict):
            logger.warning("mcp catalogue entry is not an object: %s", key)
            continue
        # The key is a human label ("公司工具网关", "Company Gateway"), so it is
        # slugged into the id the runtime uses in `mcp_<id>__<tool>`.
        slug = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")[:32]
        try:
            item = CatalogItem(**{**spec, "id": spec.get("id") or slug})
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"].removeprefix("Value error, ")
            logger.warning("mcp catalogue entry %s ignored: %s", key, reason)
            continue
        items[item.id] = item
    return list(items.values())


def public_item(item: dict, installed: set[str]) -> dict:
    """The catalogue row a browser may see.

    `tokenIn` is the one detail of the URL a user needs to act on: a service that
    takes the key as a query parameter cannot be reached with a bearer header,
    and the card has to say so instead of letting the install fail.
    """
    return {"id": item["id"], "name": item["name"], "summary": item.get("summary", ""),
            "nameEn": item.get("name_en", ""), "summaryEn": item.get("summary_en", ""),
            "category": item.get("category", "general"), "auth": item.get("auth", "token"),
            "tokenIn": "query" if TOKEN_PLACEHOLDER in item.get("url", "") else "header",
            "docs": item.get("docs", ""), "verified": bool(item.get("verified")),
            "private": bool(item.get("private")), "enabled": item.get("enabled", True),
            "installed": item["id"] in installed}
