"""Which upstream failures deserve another attempt, and how long to wait.

The provider is a remote service we do not control: it rate limits, it returns
502 while a node is rotated, it drops a connection mid-handshake. None of those
say anything about the request, so repeating it once or twice is the difference
between a visible failure and an answer the user never knew was delayed.

The failures that *do* describe the request are absent on purpose. Repeating a
401 or a 422 only burns the turn and makes the user wait for the same refusal.
"""

import email.utils
import time

import httpx

TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    return isinstance(exc, httpx.TransportError)


def _retry_after(exc: Exception) -> float | None:
    """The provider's own hint, in seconds, when it sent one."""
    response = getattr(exc, "response", None)
    raw = (response.headers.get("retry-after", "") if response is not None else "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # A date without a zone cannot be trusted to be an absolute instant; the
    # same "-0000" parse that makes a naive datetime would be off by hours.
    if when.tzinfo is None:
        return None
    return when.timestamp() - time.time()


def wait_for(exc: Exception, attempt: int, backoff: float, ceiling: float) -> float:
    """Exponential backoff, capped; the provider's Retry-After wins when present."""
    hinted = _retry_after(exc)
    if hinted is not None:
        return max(0.0, min(ceiling, hinted))
    return min(ceiling, backoff * (2 ** attempt))
