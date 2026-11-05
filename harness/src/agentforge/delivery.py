"""The one failure both verification channels share.

`auth.py` must not care whether a code travelled by SMS or by mail: either the
message left the building or it did not. Each sender wraps its provider's own
exceptions into a subclass of this, so the caller has a single thing to catch
and a reason to log.
"""


class DeliveryError(Exception):
    """A message the provider refused or could not carry."""
