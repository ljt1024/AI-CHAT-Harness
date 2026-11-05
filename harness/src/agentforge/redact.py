"""Keep credentials and tenant identifiers out of logs and API responses.

Two places leak them by accident: a hosted deployment URL carries the tenant in
the path, and an HTTP client's exception message quotes the whole URL back. Both
are handled here so every caller scrubs the same way.
"""
import re
from urllib.parse import urlsplit, urlunsplit

# ModelScope and similar hosts put an opaque hex id in the path. It is the
# credential when the deployment does not require a token.
_OPAQUE_SEGMENT = re.compile(r"^(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]{12,}$")
_URL = re.compile(r"https?://[^\s'\"`]+")


def mask_url(url: str) -> str:
    parsed = urlsplit(url)
    path = "/".join("****" if _OPAQUE_SEGMENT.match(part) else part
                    for part in parsed.path.split("/"))
    # Query and fragment are dropped: they are either meaningless or a secret.
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def leaf(exc: BaseException) -> BaseException:
    """The innermost error of a nested group.

    An async client reports a connection failure as an ExceptionGroup, whose
    message says nothing. The interesting part is the leaf.
    """
    for _ in range(8):
        nested = getattr(exc, "exceptions", None)
        if not nested:
            break
        exc = nested[0]
    return exc


def describe(exc: BaseException) -> str:
    inner = leaf(exc)
    message = str(inner) or repr(inner)
    return f"{type(inner).__name__}: {_URL.sub(lambda match: mask_url(match.group(0)), message)}"


def status_code(exc: BaseException) -> int | None:
    response = getattr(leaf(exc), "response", None)
    return getattr(response, "status_code", None)
