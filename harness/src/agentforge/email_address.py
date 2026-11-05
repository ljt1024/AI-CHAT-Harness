"""Email addresses in the shapes this codebase needs them.

One place, because an address is an identity: if two modules disagree about
whether `Alice@Example.com` and `alice@example.com` are the same account, a
person can end up with two. Lower-casing the whole address is technically
stricter than RFC 5321 (the local part may be case-sensitive), but every
consumer mailbox in practice is not, and "one address, one account" is worth
more here than the letter of the standard.

Deliberately no display-name parsing and no quoted local parts: this is a
sign-in identifier, not a mail client.
"""

import re

# Local part, one @, a dotted domain with a letters-only TLD. Rejecting the
# exotic shapes early keeps the verification mail deliverable and the database
# key comparable.
PATTERN = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,24}$")


def normalize(raw: str) -> str:
    """` Alice@Example.COM ` -> `alice@example.com`. Raises for anything else."""
    value = (raw or "").strip().lower()
    if len(value) > 254 or not PATTERN.fullmatch(value):
        raise ValueError("Enter an email address, for example name@example.com.")
    # A domain cannot contain two dots in a row or start/end with one, and the
    # same is true of the local part in every mailbox a person actually has.
    local, domain = value.split("@", 1)
    if ".." in value or local.startswith(".") or local.endswith(".") \
            or domain.startswith((".", "-")) or domain.endswith((".", "-")):
        raise ValueError("Enter an email address, for example name@example.com.")
    return value


def mask(address: str) -> str:
    """a***@example.com -- enough to recognise, not enough to harvest."""
    value = (address or "").strip().lower()
    local, _, domain = value.partition("@")
    if not local or not domain:
        return "***"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***@{domain}"


def looks_like(value: str) -> bool:
    try:
        normalize(value)
    except ValueError:
        return False
    return True
