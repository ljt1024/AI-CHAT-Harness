import json

from fastapi.testclient import TestClient
from agentforge.protocol_stub import app


def test_legacy_contract(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_PROTOCOL_STUB", "1")
    with TestClient(app) as client:
        response = client.post("/api/agents/run", json={
            "input": "verify", "sessionId": "session-1", "turnId": "turn-1", "stream": True,
        })
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["start", "delta", "done"]
    assert all(event["turnId"] == "turn-1" for event in events)
    assert events[-1]["result"]["sessionId"] == "session-1"
