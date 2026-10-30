"""Server-side accounts and sessions.

Identity has exactly one source: the session cookie. Nothing in a request body,
a query string or a model message can name a user, which is what makes the
ownership checks in `api.py` meaningful.

Passwords use `hashlib.scrypt` rather than a third-party KDF. The deployment
server builds its virtualenv without a package proxy, so a native dependency
would have to be provisioned twice; the encoded hash records its own parameters,
so switching to argon2 later is a rehash on next login and not a migration.
"""

import base64
import contextvars
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from agentforge.models import new_id

logger = logging.getLogger(__name__)

SESSION_COOKIE = "agentforge_session"
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$")
MIN_PASSWORD = 10
MAX_PASSWORD = 200
_SCRYPT = {"n": 16384, "r": 8, "p": 1, "dklen": 32}
# A wrong password and an unknown username must cost the same, otherwise the
# response time tells an attacker which half they got right.
_DUMMY_HASH = None

# The authenticated user for the current request and every task it spawns. Set
# once by the HTTP middleware; a tool, a background run or a resumed checkpoint
# inherits it and can never override it.
current_user: contextvars.ContextVar[str] = contextvars.ContextVar("agentforge_user", default="")


class AuthError(Exception):
    """A login attempt that must not reveal which half was wrong."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode(), validate=True)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=_SCRYPT["dklen"],
                            n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"])
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        salt=_b64(salt), digest=_b64(digest), **_SCRYPT)


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check that never raises on a malformed record."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), dklen=len(expected),
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    # Stored as an ISO string like every other timestamp in this database, so
    # comparisons stay plain string comparisons in the same format.
    return moment.isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, store, session_hours: float = 12, max_failures: int = 5,
                 lockout_seconds: float = 60):
        self.store = store
        self.session_hours = session_hours
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, tuple[int, float]] = {}

    async def setup(self):
        await self.store.db.users.create_index("username", unique=True)
        await self.store.db.auth_sessions.create_index("token_hash", unique=True)
        await self.store.db.auth_sessions.create_index("user_id")

    # ---------------------------------------------------------------- accounts
    async def create_user(self, username: str, password: str) -> dict:
        username = (username or "").strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError(
                "Username must start with a letter and use 3-32 letters, digits, '.', '_' or '-'.")
        if not MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD:
            raise ValueError(f"Password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters.")
        if await self.store.db.users.find_one({"username": username.lower()}):
            raise ValueError("That username is already taken.")
        user = {"id": new_id(), "username": username.lower(),
                "password": hash_password(password), "created_at": _iso(_now())}
        await self.store.db.users.insert_one(dict(user))
        return {k: v for k, v in user.items() if k != "password"}

    async def user_by_name(self, username: str):
        return await self.store.db.users.find_one({"username": (username or "").strip().lower()},
                                                 {"_id": 0})

    async def user_by_id(self, identity: str):
        return await self.store.db.users.find_one({"id": identity}, {"_id": 0})

    def _locked(self, username: str) -> bool:
        attempts, since = self._failures.get(username, (0, 0.0))
        if attempts < self.max_failures:
            return False
        if time.monotonic() - since < self.lockout_seconds:
            return True
        self._failures.pop(username, None)
        return False

    def _record_failure(self, username: str):
        attempts = self._failures.get(username, (0, 0.0))[0] + 1
        self._failures[username] = (attempts, time.monotonic())

    def clear_failures(self, username: str = ""):
        if username:
            self._failures.pop(username.strip().lower(), None)
        else:
            self._failures.clear()

    async def authenticate(self, username: str, password: str) -> dict:
        global _DUMMY_HASH
        key = (username or "").strip().lower()
        if self._locked(key):
            raise AuthError("Too many failed attempts. Wait a minute and try again.")
        user = await self.user_by_name(key) if key else None
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("agentforge-dummy-password")
        stored = user["password"] if user else _DUMMY_HASH
        if not verify_password(password or "", stored) or user is None:
            self._record_failure(key)
            raise AuthError("Wrong username or password.")
        self._failures.pop(key, None)
        return {k: v for k, v in user.items() if k != "password"}

    # ---------------------------------------------------------------- sessions
    async def start_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        expires = _now() + timedelta(hours=self.session_hours)
        await self.store.db.auth_sessions.insert_one({
            "id": new_id(), "token_hash": _hash_token(token), "user_id": user_id,
            "created_at": _iso(_now()), "expires_at": _iso(expires)})
        return token

    async def user_for_token(self, token: str):
        if not token:
            return None
        row = await self.store.db.auth_sessions.find_one({"token_hash": _hash_token(token)})
        if row is None or row.get("expires_at", "") <= _iso(_now()):
            return None
        return await self.store.db.users.find_one({"id": row["user_id"]}, {"_id": 0})

    async def end_session(self, token: str):
        if token:
            await self.store.db.auth_sessions.delete_one({"token_hash": _hash_token(token)})

    async def revoke_user(self, user_id: str) -> int:
        """Log a user out everywhere, which is what disabling an account means."""
        result = await self.store.db.auth_sessions.delete_many({"user_id": user_id})
        return result.deleted_count

    async def prune(self) -> int:
        result = await self.store.db.auth_sessions.delete_many({"expires_at": {"$lte": _iso(_now())}})
        return result.deleted_count

    # --------------------------------------------------------------- bootstrap
    async def bootstrap(self, username: str, password: str) -> dict | None:
        """Create the designated first account, if it does not exist yet.

        Deliberately never "first visitor wins": without an explicit account the
        service starts with no one able to log in, rather than one able to log in
        as somebody else.
        """
        if not username or not password:
            return None
        existing = await self.user_by_name(username)
        if existing:
            return {k: v for k, v in existing.items() if k != "password"}
        try:
            user = await self.create_user(username, password)
        except ValueError as exc:
            logger.error("Cannot create the initial account %r: %s", username, exc)
            return None
        logger.info("Created the initial account %s", user["username"])
        return user

    async def adopt_ownerless(self, user_id: str) -> dict:
        """Bind resources written before accounts existed to the first account.

        Idempotent: only documents missing `owner_id` are touched, and the count
        is logged so an operator can see what the migration moved.
        """
        counts = {}
        for collection in ("agents", "sessions", "traces", "turns", "tool_calls",
                           "checkpoints", "artifacts", "skills", "mcp_servers"):
            result = await self.store.db[collection].update_many(
                {"owner_id": {"$exists": False}}, {"$set": {"owner_id": user_id}})
            counts[collection] = result.modified_count
        if any(counts.values()):
            logger.info("Bound legacy resources to %s: %s", user_id, counts)
        return counts
