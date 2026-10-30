from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentforge.models import DEFAULT_SANDBOX_IMAGE, SandboxPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")
    deepseek_api_key: SecretStr = SecretStr("")
    mongodb_uri: str = "mongodb://localhost:27017"
    agentforge_db: str = "agentforge"
    mcp_server_url: str = "http://localhost:9000/mcp"
    # Hosted MCP services cold-start, and discovery blocks the request that
    # triggers it, so the two budgets are separate.
    mcp_discover_timeout: float = Field(default=30, ge=1, le=300)
    mcp_call_timeout: float = Field(default=60, ge=1, le=600)
    # Registration is the opt-in for servers every run may use: there is no
    # per-conversation picker in the product UI. An agent that pins an explicit
    # list still wins, and a pinned server that is down fails the run while an
    # auto-attached one is skipped.
    mcp_auto_attach: bool = True
    # Optional startup file holding an `mcpServers` block. Registration is a
    # database row with a token in it, which is a poor thing to version; a file
    # is the way to keep the list beside the code and re-apply it on restart.
    mcp_config: Path = Path("../config/mcp.json")
    agentforge_workspace: Path = Path("../.agentforge/workspaces")
    approval_timeout: float = 300
    intervention_policy: Path = Path("../config/intervention.yaml")
    sandbox_warm_size: int = Field(default=2, ge=0, le=8)
    sandbox_max_size: int = Field(default=8, ge=1, le=32)
    sandbox_idle_seconds: int = Field(default=600, ge=1)
    # The image a session starts in when it does not ask for one. Anything here
    # has to be an image SandboxPolicy admits: the wrong registry prefix would
    # otherwise give every session an arbitrary root filesystem of our choosing.
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE

    # Authentication. The service refuses every /api request without a session,
    # so an empty password means "start up with nobody able to log in" rather
    # than "let everyone in" - a deployment mistake should not become an open
    # door. The first account is created only from these two values.
    admin_user: str = "admin"
    admin_password: SecretStr = SecretStr("")
    session_hours: float = Field(default=12, gt=0, le=720)
    # Cookies need Secure behind HTTPS; loopback development over plain HTTP
    # cannot set it or the browser drops the cookie.
    cookie_secure: bool = False
    # Extra origins allowed for state-changing requests. Empty means "same origin
    # only", which is what a single-host deployment needs.
    allowed_origins: str = ""

    @field_validator("sandbox_image")
    @classmethod
    def _known_image(cls, value: str) -> str:
        try:
            SandboxPolicy(image=value)
        except Exception as exc:
            raise ValueError(f"Unknown sandbox image {value!r}.") from exc
        return value
