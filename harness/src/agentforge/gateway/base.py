from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from agentforge.models import ModelRef, ToolCall


class Pricing(BaseModel):
    input_per_million: float = 0.28
    output_per_million: float = 0.42


class ChatRequest(BaseModel):
    messages: list[dict]
    tools: list[dict] = Field(default_factory=list)
    model_ref: ModelRef = Field(default_factory=ModelRef)


class ContentDelta(BaseModel):
    type: Literal["content"] = "content"
    text: str


class ToolCallRequest(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class UsageReport(BaseModel):
    type: Literal["usage"] = "usage"
    tokens_in: int
    tokens_out: int
    usd: float = 0


class Done(BaseModel):
    type: Literal["done"] = "done"


Chunk = ContentDelta | ToolCallRequest | UsageReport | Done


class ModelAdapter(Protocol):
    def chat_stream(self, req: ChatRequest) -> AsyncIterator[Chunk]: ...

    @property
    def pricing(self) -> Pricing: ...
