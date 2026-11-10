"""Whether this process may dial a URL at all.

Two capabilities reach out from the host: the web tools and the MCP client. They
must answer the same question the same way, because the thing they are protecting
is the same -- the host's own network position. A registered MCP server is a URL
somebody else typed, and the host process sits next to MongoDB, the model gateway
and the cloud metadata endpoint, so "it is only a URL" is not a small thing.

The predicate is ``is_global``: it covers loopback, RFC1918, link-local
(including 169.254.169.254), CGNAT and reserved space in one test, so there is no
denylist here to fall behind.

``allow_private`` exists for a deployment that runs its own MCP server beside the
harness. It stays opt-in per call and never the default: the distance between
"our own sidecar" and "the metadata endpoint" is a judgement only the operator can
make. Link-local is excluded even then, because no sidecar is ever addressed
there.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# Ranges an operator may reasonably mean when it says "our own". Spelled out
# rather than `is_private`, which also covers 169.254/16 and 0.0.0.0/8.
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128",
    "fc00::/7"))


class BlockedAddress(ValueError):
    """A URL this process refuses to dial; the message names the failed part."""


def check_shape(url: str) -> str:
    """Scheme, credentials and host, before any DNS work.

    Returns the URL unchanged so a caller can chain the call.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BlockedAddress(
            f"Only http and https URLs can be fetched, not {parts.scheme or 'a relative path'}.")
    if parts.username or parts.password:
        raise BlockedAddress("Remove the credentials from the URL; this tool does not send them.")
    if not parts.hostname:
        raise BlockedAddress("That URL has no host name.")
    return url


async def resolve(host: str) -> list[str]:
    """Every address the name currently maps to.

    All of them are checked, not the first: a name that answers with one public
    and one private address is an attempt to be reached through the second.
    """
    loop = asyncio.get_running_loop()
    try:
        info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"DNS lookup failed for {host}: {exc.strerror or exc}.") from exc
    return sorted({entry[4][0] for entry in info})


async def assert_public(url: str, *, allow_private: bool = False) -> bool:
    """Raise unless every address behind `url` is one this process may reach.

    Returns whether the name resolved to a private address, which is what lets a
    caller label the row "our own sidecar" instead of guessing from the hostname.
    """
    check_shape(url)
    host = urlsplit(url).hostname or ""
    private = False
    for address in await resolve(host):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise BlockedAddress(
                f"{host} resolved to an address this tool cannot classify.") from exc
        if ip.is_global:
            continue
        if allow_private and any(ip in network for network in _PRIVATE_NETWORKS):
            private = True
            continue
        raise BlockedAddress(f"{host} resolves to {address}, which is not a public address.")
    return private
