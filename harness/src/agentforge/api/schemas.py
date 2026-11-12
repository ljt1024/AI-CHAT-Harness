"""Request and response bodies.

Kept in one place because several of them are shared by more than one router
(the approval answer is also a runtime callback, the sandbox request is used by
host tooling), and because a reader looking for "what does this endpoint
accept" should not have to search nine modules.
"""
from pydantic import BaseModel, Field, StrictBool

from agentforge.capabilities import MAX_CONFIG
from agentforge.models import new_id


class LegacyRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100000)
    sessionId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turnId: str = Field(default_factory=new_id, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    stream: bool = False
    # Continue the previous run from its latest checkpoint instead of a fresh turn.
    resume: bool = False
    agentIds: list[str] = Field(default_factory=list, max_length=1)
    model: str = "deepseek-chat"
    fileIds: list[str] = Field(default_factory=list)
    sandbox: bool = False
    skills: list[str] = Field(default_factory=list, max_length=8)
    mcp_servers: list[str] = Field(default_factory=list, max_length=8)


class SessionRequest(BaseModel):
    agent_id: str
    task: str = Field(min_length=1, max_length=100000)


class ApprovalRequest(BaseModel):
    approved: StrictBool
    reason: str = Field(default="", max_length=2000)
    # "Yes, and stop asking about this tool for the rest of this conversation."
    # Ignored for tools the gate does not consider rememberable.
    remember: bool = False


class MCPConfig(BaseModel):
    """A pasted `{"mcpServers": {...}}` block, unparsed."""
    config: str = Field(min_length=1, max_length=MAX_CONFIG)


class MCPToggle(BaseModel):
    enabled: bool


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class SmsCodeRequest(BaseModel):
    """Ask for a code. `bind` is only honoured for a signed-in caller."""

    phone: str = Field(min_length=6, max_length=24)
    purpose: str = Field(default="login", pattern=r"^(login|bind)$")


class SmsLoginRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=12)


class PhoneBindRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=12)


class EmailCodeRequest(BaseModel):
    """Ask for a code by email. `bind` is only honoured for a signed-in caller."""

    email: str = Field(min_length=5, max_length=254)
    purpose: str = Field(default="login", pattern=r"^(login|bind)$")


class EmailLoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class EmailBindRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class SandboxFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=8_000_000)


class SandboxExecRequest(BaseModel):
    """One-shot execution inside a disposable sandbox.

    Lets host-side tooling run untrusted code with the agent's isolation
    instead of the host's privileges: offline, read-only root, no capabilities.
    """

    command: list[str] = Field(min_length=1, max_length=64)
    files: list[SandboxFile] = Field(default_factory=list, max_length=200)
    timeout: float = Field(default=30, gt=0, le=600)
    image: str = Field(default="python:3.12-slim", pattern=r"^[a-zA-Z0-9._:/@-]{1,200}$")


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(max_length=100000)


class CompletionRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    model: str = "deepseek-chat"
    stream: bool = True
