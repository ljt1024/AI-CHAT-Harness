"""The plain completion door.

No tools, no sandbox: the browser's first chat page streams a model answer and
nothing else. It is kept because the frontend still uses it for chat-only
conversations, and because the OpenAI-shaped envelope is handy for smoke tests.
"""
import json

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agentforge.api.deps import AppDeps
from agentforge.api.schemas import CompletionRequest
from agentforge.gateway.base import ModelOutputError
from agentforge.gateway.deepseek import DeepSeekAdapter
from agentforge.models import ModelRef
from agentforge.prompts import with_response_language


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    gateway = d.gateway
    bill_guest_turn = d.bill_guest_turn

    @router.post("/api/chat/completions")
    async def chat(request: CompletionRequest):
        adapter = gateway.adapters.get("deepseek")
        if isinstance(adapter, DeepSeekAdapter) and not adapter.api_key:
            raise HTTPException(503, "Configure the DeepSeek API key in the Harness environment and restart.")
        if not request.stream:
            raise HTTPException(422, "Use stream=true.")
        await bill_guest_turn()
        async def frames():
            try:
                user_request = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
                messages = with_response_language([m.model_dump() for m in request.messages], user_request)
                async for chunk in gateway.chat(messages, [], ModelRef(model_id=request.model)):
                    if chunk.type == "content":
                        payload = {"choices": [{"delta": {"content": chunk.text}}]}
                    elif chunk.type == "usage":
                        payload = {"choices": [], "usage": {"prompt_tokens": chunk.tokens_in,
                            "completion_tokens": chunk.tokens_out, "total_tokens": chunk.tokens_in + chunk.tokens_out}}
                    else:
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
            except ModelOutputError as exc:
                chinese = any("\u3400" <= c <= "\u9fff" for c in user_request)
                message = ("模型输出达到长度限制，回答尚未完成。请缩小任务范围或要求分段输出。" if chinese
                           else "Model output reached its length limit. Request a smaller response or split the task.")
                yield f"data: {json.dumps({'error': {'message': message, 'code': exc.code}})}\n\n"
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                message = {
                    401: "Model provider rejected the API key. Check DEEPSEEK_API_KEY and restart.",
                    402: "Model provider balance is insufficient. Check your provider account.",
                    429: "Model provider rate limit reached. Please retry later.",
                }.get(status, f"Model provider request failed (HTTP {status}). Please retry later.")
                yield f"data: {json.dumps({'error': {'message': message}})}\n\n"
                return
            except httpx.TransportError:
                yield f"data: {json.dumps({'error': {'message': 'Model connection interrupted. Please retry.'}})}\n\n"
                return
            yield "data: [DONE]\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream")

    return router
