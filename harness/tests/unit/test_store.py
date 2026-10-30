from mongomock_motor import AsyncMongoMockClient

from agentforge.store import MongoDBStore


async def test_persistence_and_checkpoint_isolation():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    await store.setup()
    await store.put("sessions", {"id": "s", "sessionId": "s", "turnId": "t"})
    assert (await store.get("sessions", "s"))["turnId"] == "t"
    state = {"history": [{"role": "user", "content": "hello"}], "lastTurnId": "t"}
    await store.checkpoint("s", state)
    state["history"].clear()
    saved = await store.latest_checkpoint("s")
    assert saved["state"]["history"][0]["content"] == "hello"
    assert await store.latest_checkpoint("other") is None
    await store.put("sessions", {"id": "s", "sessionId": "s", "turnId": "t2"})
    assert len(await store.list("sessions")) == 1
