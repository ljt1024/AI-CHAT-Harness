from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


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


DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"

# The agent every session starts from when none is named. Its row is shared
# read-only: it carries an empty owner, which means every account — including a
# visitor — can read and run it, and nobody can replace it. Never bind it to an
# account: doing so is invisible in the admin's own session and only shows up
# as "Agent not found." for everyone else.
DEFAULT_AGENT_ID = "default"

# Images that can run the integrated file tools: anything with a CPython
# interpreter. `node:22-slim` has no python, so sessions pinned to it may not
# use write_file/read_file and friends.
PYTHON_SANDBOX_IMAGES = ("python:3.12-slim", "agentforge/python-docs:3.12")


def default_sandbox_image() -> str:
    """The image a session starts in when it does not name one.

    Resolved late, because the settings module imports this one. A machine that
    built the document image can say so once instead of every agent repeating it.
    """
    from agentforge.config import Settings

    return Settings().sandbox_image


class SandboxPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # `agentforge/python-docs:3.12` is built from harness/docker/sandbox-docs: the
    # same interpreter carrying the document libraries, because a sandbox has no
    # network to install them into itself.
    image: Literal["python:3.12-slim", "node:22-slim", "agentforge/python-docs:3.12"] = Field(
        default_factory=default_sandbox_image)
    timeout: int = Field(default=30, ge=1, le=30)


class AgentDefinition(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = "Complete the task. Use tools only when needed."
    model: ModelRef = Field(default_factory=ModelRef)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, max_length=8)
    mcp_servers: list[str] = Field(default_factory=list, max_length=8)
    sandbox_policy: SandboxPolicy | None = None
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
    # How much of `output` this tool is allowed to keep. A remote service often
    # returns one large document (a product list, a page of JSON), and cutting it
    # at the local default leaves the caller with a half sentence it cannot parse,
    # so the tool that knows the shape sets its own ceiling. Local defaults stay
    # tight: shell and file tools gain nothing from a bigger slice.
    limit: int | None = Field(default=None, exclude=True)


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
