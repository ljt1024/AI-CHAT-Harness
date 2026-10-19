from copy import deepcopy
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from agentforge.models import new_id


class MongoDBStore:
    def __init__(self, uri: str, database: str, client=None):
        self.client = client if client is not None else AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=3000)
        self.db = self.client[database]

    async def setup(self):
        for name in ("agents", "sessions", "traces", "turns", "tool_calls", "checkpoints", "approvals"):
            await self.db[name].create_index("id", unique=True)
        await self.db.traces.create_index("sessionId")
        await self.db.turns.create_index([("trace_id", 1), ("index", 1)], unique=True)

    async def put(self, collection: str, document: dict):
        data = deepcopy(document)
        data.setdefault("id", new_id())
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self.db[collection].replace_one({"id": data["id"]}, data, upsert=True)
        return {k: v for k, v in data.items() if k != "_id"}

    async def get(self, collection: str, identity: str):
        return await self.db[collection].find_one({"id": identity}, {"_id": 0})

    async def list(self, collection: str, query: dict | None = None):
        return await self.db[collection].find(query or {}, {"_id": 0}).to_list(length=10000)

    async def checkpoint(self, session_id: str, state: dict):
        return await self.put("checkpoints", {
            "id": new_id(), "sessionId": session_id, "state": deepcopy(state)})

    async def latest_checkpoint(self, session_id: str):
        return await self.db.checkpoints.find_one(
            {"sessionId": session_id}, {"_id": 0}, sort=[("updated_at", -1)])
