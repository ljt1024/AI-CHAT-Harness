"""Explicitly opt-in transport stub; never used by the production runtime."""
import json
import os
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="AgentForge")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


class LegacyRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100000)
    sessionId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turnId: str = Field(default_factory=lambda: str(uuid4()), pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    stream: bool = False


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "protocol-stub"}


@app.post("/api/agents/run")
async def run(request: LegacyRequest):
    if os.getenv("AGENTFORGE_PROTOCOL_STUB") != "1":
        raise HTTPException(503, "Protocol stub disabled; configure the runtime.")
    result = {"id": str(uuid4()), "sessionId": request.sessionId, "input": request.input,
              "output": "AgentForge protocol connection verified.", "steps": [], "memoryMessages": 2}
    if not request.stream:
        return {"code": 200, "data": result, "msg": "ok"}

    async def events():
        for event in [{"type": "start", "sessionId": request.sessionId, "memoryMessages": 0},
                      {"type": "delta", "text": result["output"]},
                      {"type": "done", "result": result}]:
            event.update(turnId=request.turnId, trace_id=result["id"])
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")
