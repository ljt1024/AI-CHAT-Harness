"""Credentials at rest.

An MCP bearer token outlives the request that created it. Left in a document,
it is readable from a backup, a `mongosh` session, a support dump or a replica
member -- and it is the whole credential for a hosted service. Fernet gives
authenticated encryption with a key the database never sees.

Key sources, in order:

* ``MCP_SECRET_KEY`` from the environment. Required as soon as more than one
  process or host has to read the same rows.
* A generated key file beside the workspace (0600, created once). A single-host
  deployment should not be able to leave tokens in the clear merely because it
  was configured in a hurry; the file then becomes part of what has to be backed
  up with the database.

Rows written before this module existed carry no prefix. They are read as
plaintext so an upgrade does not lose servers, and ``reencrypt`` rewrites them
once at startup.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
KEY_FILE = ".mcp-key"


def _fernet(secret: str) -> Fernet:
    """A Fernet key from any configured secret.

    An operator's value is a password, not necessarily 32 url-safe bytes, so it
    is hashed into one instead of being rejected for its shape.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def load_key(workspace: Path, secret: str = "") -> Fernet:
    """The process-wide cipher, from the environment or a 0600 file."""
    if secret.strip():
        return _fernet(secret.strip())
    path = workspace / KEY_FILE
    try:
        return _fernet(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"MCP credential key is unreadable: {path}") from exc
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two workers starting together must not silently pick different
        # keys, which would make each other's rows undecryptable.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
    except FileExistsError:
        key = path.read_text(encoding="utf-8").strip()
    return _fernet(key)


class Cipher:
    """Encrypt on the way in, decrypt on the way out, tolerate old plaintext."""

    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    def is_encrypted(self, value: str) -> bool:
        return value.startswith(PREFIX)

    def encrypt(self, value: str | None) -> str | None:
        if not value or self.is_encrypted(value):
            return value
        return PREFIX + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if not value or not self.is_encrypted(value):
            return value
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            # A rotated key is an operator action, and pretending the token is
            # absent would turn into "the service rejected your token" three
            # layers later. Say what actually happened.
            raise ValueError("MCP credential cannot be decrypted with the configured key.") from exc


_current: Cipher | None = None


def configure(workspace: Path, secret: str = "") -> Cipher:
    """Called once at startup; the cipher is process-wide on purpose."""
    global _current
    _current = Cipher(load_key(workspace, secret))
    return _current


def cipher() -> Cipher:
    if _current is None:
        raise RuntimeError("MCP credential cipher is not configured.")
    return _current


def encrypt(value: str | None) -> str | None:
    return cipher().encrypt(value)


def decrypt(value: str | None) -> str | None:
    # Not configured means a caller outside the app (a script, a unit test) that
    # only ever handled plaintext; there is nothing to undo, so pass it through
    # rather than making every such caller configure a key first.
    if not value or _current is None:
        return value
    return _current.decrypt(value)
