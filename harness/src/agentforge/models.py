from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid4())


class ModelRef(BaseModel):
    provider: str = "deepseek"
    model_id: str = "deepseek-chat"
    params: dict[str, Any] = Field(default_factory=dict)


class StopConditions(BaseModel):
    max_turns: int = Field(default=25, ge=1, le=100)
    budget: float = Field(default=0.5, gt=0, le=100)
    idle_limit: int = Field(default=3, ge=1, le=10)


class AgentDefinition(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = "Complete the task. Use tools only when needed."
    model: ModelRef = Field(default_factory=ModelRef)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    sandbox_policy: dict | None = None
    permissions: dict[str, Literal["auto", "rule", "confirm"]] = Field(default_factory=dict)
    stop_conditions: StopConditions = Field(default_factory=StopConditions)


class ToolCall(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    arguments: dict = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    output: str = ""
    error: str | None = None


class Cost(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0


class RunResult(BaseModel):
    trace_id: str
    session_id: str
    turn_id: str
    output: str = ""
    stop_reason: str
    cost: Cost = Field(default_factory=Cost)
