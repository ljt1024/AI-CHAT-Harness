"""The service catalogue a deployment offers its users.

The point of a catalogue is not the list -- it is who typed the URL. Registering
an MCP server makes the *host* dial an address, so a user-supplied string is a
request to use the host's network position, while a catalogue entry is an address
the operator already chose. Installing from here therefore cannot be turned into
a probe of 127.0.0.1 or the metadata endpoint, and the user never has to know
what a Streamable HTTP endpoint looks like.

A deployment fills this in from whatever it standardises on: an internal MCP
gateway, a vendor's hosted server, one sidecar per team. Entries are data, so a
broken one is logged and skipped rather than taking the whole file down.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_ITEMS = 64
MAX_FILE = 200_000


class CatalogItem(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=300)
    category: str = Field(default="general", max_length=40)
    url: str = Field(max_length=2000)
    # "none" is a server the deployment runs for everyone; "token" means each
    # account brings its own credential, which stays in that account's row.
    auth: Literal["none", "token"] = "token"
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
                or parsed.username or parsed.password or parsed.fragment or parsed.query):
            raise ValueError("Use an HTTP(S) MCP URL without credentials, query or fragment.")
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
    """The catalogue row a browser may see."""
    return {"id": item["id"], "name": item["name"], "summary": item.get("summary", ""),
            "category": item.get("category", "general"), "auth": item.get("auth", "token"),
            "docs": item.get("docs", ""), "verified": bool(item.get("verified")),
            "private": bool(item.get("private")), "enabled": item.get("enabled", True),
            "installed": item["id"] in installed}
