"""Chinese mobile numbers in the shapes this codebase needs them.

One place, because a phone number is an identity: if two modules disagree about
whether `+86 138-0013-8000` and `13800138000` are the same account, a person can
end up with two.
"""

import re

# Deliberately the mainland mobile shape only. Anything else -- landlines,
# overseas numbers, short codes -- would need per-provider rules for delivery
# and a different template, neither of which this deployment has.
PATTERN = re.compile(r"^1[3-9]\d{9}$")


def normalize(raw: str) -> str:
    """`+86 138-0013-8000` -> `+8613800138000`. Raises for anything else."""
    digits = re.sub(r"[\s()\-]", "", (raw or "").strip())
    for prefix in ("+86", "0086", "86"):
        if digits.startswith(prefix) and len(digits) == len(prefix) + 11:
            digits = digits[len(prefix):]
            break
    if not PATTERN.fullmatch(digits):
        raise ValueError("Enter a mainland Chinese mobile number, 11 digits.")
    return f"+86{digits}"


def local(phone: str) -> str:
    """The 11 digits, which is what a carrier template expects."""
    return phone.removeprefix("+86")


def mask(phone: str) -> str:
    """138****8000 -- enough to recognise, not enough to call."""
    digits = local(phone)
    return f"{digits[:3]}****{digits[-4:]}" if len(digits) == 11 else "***"
