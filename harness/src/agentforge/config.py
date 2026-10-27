from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    agentforge_workspace: Path = Path("../.agentforge/workspaces")
    approval_timeout: float = 300
    intervention_policy: Path = Path("../config/intervention.yaml")
    sandbox_warm_size: int = Field(default=2, ge=0, le=8)
    sandbox_max_size: int = Field(default=8, ge=1, le=32)
    sandbox_idle_seconds: int = Field(default=600, ge=1)
