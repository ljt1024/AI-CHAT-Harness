from copy import deepcopy
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from agentforge.auth import current_user
from agentforge.models import new_id

# Collections whose rows belong to one account. Uniqueness is per owner, so two
# users can install the same skill content or register a server with the same
# name without overwriting each other.
OWNED_COLLECTIONS = ("agents", "sessions", "traces", "turns", "tool_calls", "checkpoints",
                     "artifacts", "skills", "mcp_servers")


class MongoDBStore:
    def __init__(self, uri: str, database: str, client=None):
        self.client = client if client is not None else AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=3000)
        self.db = self.client[database]

    async def setup(self):
        for name in (*OWNED_COLLECTIONS, "approvals", "users", "auth_sessions"):
            await self._owner_index(name)
        await self.db.traces.create_index("sessionId")
        await self.db.turns.create_index([("trace_id", 1), ("index", 1)], unique=True)
        await self.db.users.create_index("username", unique=True)
        await self.db.auth_sessions.create_index("token_hash", unique=True)

    async def _owner_index(self, collection: str):
        """Make (owner_id, id) the key, and retire the old global id index.

        A database created before accounts existed carries a unique index on
        `id` alone, which would reject the second user's copy of a resource.
        Dropping a missing index raises, and is the normal case on a fresh
        database, so it is intentionally swallowed.
        """
        try:
            await self.db[collection].drop_index("id_1")
        except Exception:
            pass
        await self.db[collection].create_index([("owner_id", 1), ("id", 1)], unique=True)

    def scope(self, owner: str | None = None) -> dict:
        if owner is None:
            owner = current_user.get()
        # An empty owner means a system path: startup, migration, a health probe.
        # Only the explicitly shared resources (the built-in agent) carry ""
        # as a stored owner, so this also lets every account read them.
        return {"owner_id": {"$in": [owner, ""]}} if owner else {}

    async def put(self, collection: str, document: dict, owner: str | None = None):
        data = deepcopy(document)
        data.setdefault("id", new_id())
        if owner is None:
            owner = current_user.get()
        # Always materialize the field: a missing one is not the same row as "",
        # and the upsert filter would insert a duplicate on the next write.
        data["owner_id"] = owner or data.get("owner_id") or ""
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self.db[collection].replace_one(
            {"id": data["id"], "owner_id": data["owner_id"]}, data, upsert=True)
        return {k: v for k, v in data.items() if k != "_id"}

    async def get(self, collection: str, identity: str, owner: str | None = None):
        return await self.db[collection].find_one({"id": identity, **self.scope(owner)}, {"_id": 0})

    async def list(self, collection: str, query: dict | None = None, owner: str | None = None):
        return await self.db[collection].find({**(query or {}), **self.scope(owner)},
            {"_id": 0}).to_list(length=10000)

    async def checkpoint(self, session_id: str, state: dict):
        return await self.put("checkpoints", {
            "id": new_id(), "sessionId": session_id, "state": deepcopy(state)})

    async def latest_checkpoint(self, session_id: str):
        return await self.db.checkpoints.find_one(
            {"sessionId": session_id}, {"_id": 0}, sort=[("updated_at", -1)])
