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

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from agentforge.models import DEFAULT_AGENT_ID, new_id

logger = logging.getLogger(__name__)

SESSION_COOKIE = "agentforge_session"
# Anonymous visitors get an owner id of their own so the ownership filters in
# `store.py` keep working unchanged: a guest transcript is owned by the guest,
# not by nobody, which is what makes "sign in to keep this conversation" a
# transfer rather than a lookup with no owner to match.
GUEST_COOKIE = "agentforge_guest"
GUEST_PREFIX = "guest_"
_GUEST_ID = re.compile(r"^guest_[A-Za-z0-9_-]{16,64}$")
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


def new_guest_id() -> str:
    return GUEST_PREFIX + secrets.token_urlsafe(18)


def is_guest(identity: str | None) -> bool:
    return bool(identity and identity.startswith(GUEST_PREFIX))


def valid_guest_id(value: str | None) -> bool:
    """A cookie value is only trusted after this shape check.

    There is nothing to forge here: an invented id is simply a fresh, empty
    visitor. The check exists so a malformed cookie cannot become a database
    key or leak into anything that treats ids as identifiers.
    """
    return bool(value and _GUEST_ID.match(value))


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
        # One counter row per visitor per day; the unique index is what makes the
        # increment safe when two tabs of the same visitor start a turn together.
        await self.store.db.guest_usage.create_index([("id", 1), ("day", 1)], unique=True)

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

    # ----------------------------------------------------------- anonymous trial
    async def claim_guest_turn(self, guest_id: str, per_guest: int, daily: int) -> str | None:
        """Reserve one anonymous turn; return a refusal reason, or None if granted.

        The turn is charged before the run and handed back when refused, so a
        visitor who hits the wall is not also billed for asking. Two counters:
        the visitor's own budget, and a site-wide daily one -- clearing a cookie
        makes a new visitor, so the per-visitor limit alone is not a budget.
        """
        if per_guest <= 0:
            return "disabled"
        day = _iso(_now())[:10]
        used = await self._bump_guest(guest_id, day)
        if used > per_guest:
            await self._bump_guest(guest_id, day, -1)
            return "visitor"
        if daily > 0 and await self._bump_guest("*", day) > daily:
            await self._bump_guest("*", day, -1)
            await self._bump_guest(guest_id, day, -1)
            return "daily"
        return None

    async def _bump_guest(self, key: str, day: str, step: int = 1) -> int:
        query = {"id": key, "day": day}
        try:
            row = await self.store.db.guest_usage.find_one_and_update(
                query, {"$inc": {"turns": step}}, upsert=True,
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            # Two tabs of one visitor raced on the upsert; the second one retries
            # against the row the first created.
            row = await self.store.db.guest_usage.find_one_and_update(
                query, {"$inc": {"turns": step}}, return_document=ReturnDocument.AFTER)
        return int(row.get("turns", 0)) if row else 0

    async def guest_budget(self, guest_id: str, per_guest: int, daily: int) -> dict:
        """What the interface needs to say "3 free questions left"."""
        if per_guest <= 0 or not valid_guest_id(guest_id):
            return {"allowed": False, "turnsLeft": 0, "turnsTotal": 0}
        day = _iso(_now())[:10]
        left = max(0, per_guest - await self._guest_turns(guest_id, day))
        if daily > 0:
            left = min(left, max(0, daily - await self._guest_turns("*", day)))
        return {"allowed": True, "turnsLeft": left, "turnsTotal": per_guest}

    async def _guest_turns(self, key: str, day: str) -> int:
        row = await self.store.db.guest_usage.find_one({"id": key, "day": day})
        return int(row.get("turns", 0)) if row else 0

    async def adopt_guest(self, guest_id: str, user_id: str) -> dict:
        """Hand an anonymous transcript to the account that just signed in.

        The guest id comes from that browser's own cookie, so an account cannot
        arrive holding somebody else's trial. Only what a guest can create moves:
        agents, skills and MCP servers are behind the login wall already.
        """
        if not valid_guest_id(guest_id) or not user_id:
            return {}
        counts = {}
        for collection in ("sessions", "traces", "turns", "tool_calls",
                           "checkpoints", "artifacts"):
            rows = await self.store.db[collection].find(
                {"owner_id": guest_id}, {"id": 1}).to_list(length=10000)
            ids = [row["id"] for row in rows if row.get("id")]
            if not ids:
                continue
            # An id collision means this browser was signed in here before. The
            # guest rows are the ones on screen, so they keep the id.
            await self.store.db[collection].delete_many(
                {"owner_id": user_id, "id": {"$in": ids}})
            result = await self.store.db[collection].update_many(
                {"owner_id": guest_id, "id": {"$in": ids}}, {"$set": {"owner_id": user_id}})
            counts[collection] = result.modified_count
        await self.store.db.guest_usage.delete_many({"id": guest_id})
        if any(counts.values()):
            logger.info("Guest %s signed in as %s; moved %s", guest_id, user_id, counts)
        return counts

    async def prune_guests(self, retention_hours: float) -> dict:
        """Delete anonymous transcripts nobody ever claimed."""
        cutoff = _iso(_now() - timedelta(hours=retention_hours))[:10]
        stale = await self.store.db.guest_usage.find(
            {"day": {"$lt": cutoff}, "id": {"$ne": "*"}}, {"id": 1}).to_list(length=10000)
        ids = [row["id"] for row in stale if valid_guest_id(row.get("id", ""))]
        counts = {}
        if ids:
            for collection in ("sessions", "traces", "turns", "tool_calls",
                               "checkpoints", "artifacts"):
                result = await self.store.db[collection].delete_many(
                    {"owner_id": {"$in": ids}})
                if result.deleted_count:
                    counts[collection] = result.deleted_count
            logger.info("Pruned %d abandoned guest transcripts: %s", len(ids), counts)
        await self.store.db.guest_usage.delete_many({"day": {"$lt": cutoff}})
        return counts

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

        The built-in agent is left alone. It has no owner on purpose — every
        account and every visitor may run it — and adopting it here would turn
        it into one account's private resource without that account noticing
        anything wrong.
        """
        counts = {}
        for collection in ("agents", "sessions", "traces", "turns", "tool_calls",
                           "checkpoints", "artifacts", "skills", "mcp_servers"):
            query: dict = {"owner_id": {"$exists": False}}
            if collection == "agents":
                query["id"] = {"$ne": DEFAULT_AGENT_ID}
            result = await self.store.db[collection].update_many(
                query, {"$set": {"owner_id": user_id}})
            counts[collection] = result.modified_count
        # The shared agent is marked shared rather than left ownerless: a row
        # with no field at all matches nothing an account may read, and the
        # startup write then inserts a second row beside it.
        await self.store.db.agents.update_many(
            {"id": DEFAULT_AGENT_ID, "owner_id": {"$exists": False}},
            {"$set": {"owner_id": ""}})
        if any(counts.values()):
            logger.info("Bound legacy resources to %s: %s", user_id, counts)
        return counts
