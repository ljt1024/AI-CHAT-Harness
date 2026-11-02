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
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from agentforge import phone as phone_number
from agentforge.models import DEFAULT_AGENT_ID, new_id
from agentforge.sms import SmsError, SmsMessage

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
# A code is guessed by asking again, not by reading a dump, so the ceiling on
# wrong answers matters more than the length of the secret.
CODE_ATTEMPTS = 5
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


class SmsCodeError(Exception):
    """A refused code request or check.

    Carries a machine-readable reason instead of a sentence so the interface can
    answer in the reader's language; `retry_after` lets it start a countdown
    instead of saying "too often" to someone who will just click again.
    """

    def __init__(self, reason: str, retry_after: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


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


def _expired(moment) -> bool:
    if not isinstance(moment, datetime):
        return True
    return moment.replace(tzinfo=None) <= _now().replace(tzinfo=None)


def _code_matches(row: dict, code: str) -> bool:
    try:
        salt = _unb64(row["salt"])
    except (KeyError, ValueError, TypeError):
        return False
    expected = hashlib.sha256(salt + (code or "").strip().encode()).hexdigest()
    return hmac.compare_digest(expected, row.get("digest", ""))


class AuthService:
    def __init__(self, store, session_hours: float = 12, max_failures: int = 5,
                 lockout_seconds: float = 60, code_ttl: int = 300, cooldown: int = 60,
                 per_phone: int = 10, per_ip: int = 30):
        self.store = store
        self.session_hours = session_hours
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.code_ttl = code_ttl
        self.cooldown = cooldown
        self.per_phone = per_phone
        self.per_ip = per_ip
        self._failures: dict[str, tuple[int, float]] = {}

    async def setup(self):
        await self.store.db.users.create_index("username", unique=True)
        # One account per number. Sparse, because accounts made with a password
        # have no phone at all and must not collide with each other on null.
        await self.store.db.users.create_index("phone", unique=True, sparse=True)
        await self.store.db.auth_sessions.create_index("token_hash", unique=True)
        await self.store.db.auth_sessions.create_index("user_id")
        # One counter row per visitor per day; the unique index is what makes the
        # increment safe when two tabs of the same visitor start a turn together.
        await self.store.db.guest_usage.create_index([("id", 1), ("day", 1)], unique=True)
        await self.store.db.sms_codes.create_index([("phone", 1), ("purpose", 1),
                                                    ("created_at", -1)])
        # A real Date rather than the ISO string used everywhere else, because
        # this is the one column Mongo itself has to expire.
        await self.store.db.sms_codes.create_index("expire_at", expireAfterSeconds=0)
        await self.store.db.sms_sends.create_index([("id", 1), ("day", 1)], unique=True)

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
        user = {"id": new_id(), "username": username.lower(), "display_name": username,
                "password": hash_password(password), "created_at": _iso(_now())}
        await self.store.db.users.insert_one(dict(user))
        return {k: v for k, v in user.items() if k != "password"}

    async def user_by_name(self, username: str):
        return await self.store.db.users.find_one({"username": (username or "").strip().lower()},
                                                 {"_id": 0})

    async def user_by_id(self, identity: str):
        return await self.store.db.users.find_one({"id": identity}, {"_id": 0})

    async def user_by_phone(self, phone: str):
        return await self.store.db.users.find_one({"phone": phone}, {"_id": 0})

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
        # Accounts created from a phone number have no password at all. They must
        # cost the same as a wrong one, or "is this a phone account" becomes
        # readable from the response time.
        stored = (user or {}).get("password") or _DUMMY_HASH
        if not verify_password(password or "", stored) or user is None or "password" not in user:
            self._record_failure(key)
            raise AuthError("Wrong username or password.")
        self._failures.pop(key, None)
        return {k: v for k, v in user.items() if k != "password"}

    # ------------------------------------------------------------ phone accounts
    async def create_phone_user(self, number: str) -> dict:
        """Turn a verified number into an account.

        The username is derived from the number, so there is nothing for the new
        owner to choose and nothing to collide on but the number itself; the
        masked number is the display name, which is what the sidebar shows.
        """
        # Checked before inserting rather than relying on the unique index: the
        # index is the race guard, a second tab pressing "sign in" is the common
        # case, and it should not surface as an error.
        existing = await self.user_by_phone(number)
        if existing:
            return existing
        base = "p" + phone_number.local(number)[1:]
        for attempt in range(3):
            username = base if attempt == 0 else f"{base}{secrets.token_hex(2)}"
            user = {"id": new_id(), "username": username, "phone": number,
                    "display_name": phone_number.mask(number), "created_at": _iso(_now())}
            try:
                await self.store.db.users.insert_one(dict(user))
            except DuplicateKeyError:
                # Either this number registered in another tab a moment ago, or
                # an unrelated account already owns the derived username.
                existing = await self.user_by_phone(number)
                if existing:
                    return existing
                continue
            logger.info("Registered %s", phone_number.mask(number))
            return user
        raise SmsCodeError("phone_taken")

    async def bind_phone(self, user_id: str, number: str) -> dict:
        existing = await self.user_by_phone(number)
        if existing and existing["id"] != user_id:
            raise SmsCodeError("phone_taken")
        # No display-name write here, deliberately: an account made from a
        # number already shows the mask, and every other account already has a
        # name it chose -- binding a phone is not a rename.
        await self.store.db.users.update_one({"id": user_id}, {"$set": {"phone": number}})
        return {**(await self.user_by_id(user_id) or {}), "phone": number}

    # ---------------------------------------------------------- verification codes
    async def issue_code(self, raw_phone: str, purpose: str, ip: str, sender) -> dict:
        """Send one code; return what the interface needs to start a countdown.

        The row is written before the message goes out, so two requests racing on
        the same number cannot both pass the cooldown check. A failed send takes
        the row and the counters back, otherwise a provider outage would lock a
        person out of trying again.
        """
        number = phone_number.normalize(raw_phone)
        now = _now()
        previous = await self.store.db.sms_codes.find_one(
            {"phone": number, "purpose": purpose}, {"created_at": 1},
            sort=[("created_at", -1)])
        if previous:
            elapsed = (now - datetime.fromisoformat(previous["created_at"])).total_seconds()
            if elapsed < self.cooldown:
                raise SmsCodeError("cooldown", math.ceil(self.cooldown - elapsed))
        day = _iso(now)[:10]
        await self._spend_send(number, day, self.per_phone)
        if ip:
            await self._spend_send(f"ip:{ip}", day, self.per_ip)

        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        row = {"id": new_id(), "phone": number, "purpose": purpose,
               "salt": _b64(salt),
               # Not a password: a six-digit secret is guessable offline whatever
               # we do. The salt is here so a database dump or a log line does
               # not hand somebody a code that is still valid.
               "digest": hashlib.sha256(salt + code.encode()).hexdigest(),
               "attempts": 0, "sent_from": ip, "created_at": _iso(now),
               "expire_at": now + timedelta(seconds=self.code_ttl)}
        await self.store.db.sms_codes.insert_one(dict(row))
        try:
            await sender.send(SmsMessage(phone=number, code=code,
                                         minutes=max(1, self.code_ttl // 60)))
        except SmsError as exc:
            await self.store.db.sms_codes.delete_one({"id": row["id"]})
            await self._bump("sms_sends", number, day, -1)
            if ip:
                await self._bump("sms_sends", f"ip:{ip}", day, -1)
            logger.error("No SMS reached %s: %s", phone_number.mask(number), exc)
            raise SmsCodeError("send_failed") from None
        return {"expiresIn": self.code_ttl, "cooldown": self.cooldown, "code": code}

    async def verify_code(self, raw_phone: str, purpose: str, code: str) -> str:
        """Check a code and burn it. Returns the normalised number on success.

        Every wrong answer is counted, and the code dies with the attempt that
        exceeds the ceiling: a six-digit secret survives a few seconds of
        scripted guessing, not five minutes of it.
        """
        number = phone_number.normalize(raw_phone)
        row = await self.store.db.sms_codes.find_one(
            {"phone": number, "purpose": purpose}, sort=[("created_at", -1)])
        if row is None:
            raise SmsCodeError("expired")
        if _expired(row.get("expire_at")) or int(row.get("attempts", 0)) >= CODE_ATTEMPTS:
            await self.store.db.sms_codes.delete_one({"_id": row["_id"]})
            raise SmsCodeError("expired" if _expired(row.get("expire_at")) else "too_many")
        if not _code_matches(row, code):
            updated = await self.store.db.sms_codes.find_one_and_update(
                {"_id": row["_id"]}, {"$inc": {"attempts": 1}},
                return_document=ReturnDocument.AFTER)
            if int((updated or {}).get("attempts", 0)) >= CODE_ATTEMPTS:
                await self.store.db.sms_codes.delete_one({"_id": row["_id"]})
                raise SmsCodeError("too_many")
            raise SmsCodeError("invalid")
        await self.store.db.sms_codes.delete_one({"_id": row["_id"]})
        return number

    async def _spend_send(self, key: str, day: str, limit: int):
        if await self._bump("sms_sends", key, day) > limit:
            await self._bump("sms_sends", key, day, -1)
            now = _now()
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                         microsecond=0)
            raise SmsCodeError("daily_limit", int((midnight - now).total_seconds()))

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
        return await self._bump("guest_usage", key, day, step, field="turns")

    async def _bump(self, collection: str, key: str, day: str, step: int = 1,
                    field: str = "count") -> int:
        """Increment one daily counter, creating it if this is the first.

        The unique index is what makes this safe for two tabs of one visitor at
        once: the loser of the upsert race retries against the row that won.
        """
        query = {"id": key, "day": day}
        update = {"$inc": {field: step}}
        try:
            row = await self.store.db[collection].find_one_and_update(
                query, update, upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            row = await self.store.db[collection].find_one_and_update(
                query, update, return_document=ReturnDocument.AFTER)
        return int((row or {}).get(field, 0))

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

    async def first_owner(self) -> str | None:
        """The oldest account, used to settle rows left over from before accounts.

        Only consulted when the environment no longer names the operator: an
        installation with accounts but no ADMIN_USER still has to decide who owns
        the resources written by an earlier boot. Nobody can log in otherwise --
        a row whose owner matches no account is unreadable by every account.
        """
        user = await self.store.db.users.find_one({}, {"id": 1}, sort=[("_id", 1)])
        return (user or {}).get("id")

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
