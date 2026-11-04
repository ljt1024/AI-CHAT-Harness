from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentforge.models import DEFAULT_SANDBOX_IMAGE, SandboxPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")
    deepseek_api_key: SecretStr = SecretStr("")
    # A remote model fails transiently often enough that one 429 or 502 should
    # not end the user's turn. Retries stop at the first streamed chunk, so the
    # budget buys a second chance rather than a duplicated answer.
    model_retries: int = Field(default=2, ge=0, le=5)
    model_retry_backoff: float = Field(default=1.0, ge=0, le=30)
    model_retry_max_backoff: float = Field(default=8.0, ge=0, le=60)
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
    # Uploads are the one place where a request body is written to disk, so the
    # three limits that bound it are configuration, not constants: an operator
    # with small disks and an operator serving data files want different numbers.
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    upload_max_files: int = Field(default=20, ge=1, le=200)
    upload_max_bytes_per_owner: int = Field(default=200 * 1024 * 1024, ge=1024)
    approval_timeout: float = 300
    intervention_policy: Path = Path("../config/intervention.yaml")
    sandbox_warm_size: int = Field(default=2, ge=0, le=8)
    sandbox_max_size: int = Field(default=8, ge=1, le=32)
    # One account may not occupy the whole pool: a single user opening many
    # conversations would otherwise starve everyone else of containers.
    sandbox_max_per_owner: int = Field(default=4, ge=1, le=32)
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

    # Anonymous trial. A visitor may browse and run a few real turns before the
    # interface asks for an account, so the login wall sits where it costs the
    # visitor something rather than at the front door. `guest_turns=0` turns the
    # whole thing off and puts every request back behind the login wall.
    guest_turns: int = Field(default=4, ge=0, le=100)
    # A ceiling for the whole site, not per visitor: a cleared cookie is a new
    # visitor, and without this one person could spend the operator's budget by
    # refreshing. 0 means "no daily cap" and is only sane on a private network.
    guest_daily_turns: int = Field(default=200, ge=0)
    # Anonymous transcripts are deleted after this long. They belong to nobody,
    # so keeping them indefinitely would just hoard the operator's database.
    guest_retention_hours: float = Field(default=72, gt=0, le=720)

    # Sign-up and sign-in by phone. Mail is the default door (see MAIL_* below);
    # SMS stays available for deployments that prefer it, and both build on the
    # same verification-code policy. The username/password pair stays for the
    # account `bootstrap()` creates and for anyone who bound one before.
    sms_provider: str = "console"
    sms_secret_id: SecretStr = SecretStr("")
    sms_secret_key: SecretStr = SecretStr("")
    # Tencent's SmsSdkAppId; Aliyun uses an AccessKey instead and leaves it empty.
    sms_app_id: str = ""
    sms_sign_name: str = ""
    # Tencent calls it TemplateId, Aliyun TemplateCode. One deployment, one
    # provider, so one setting with two spellings of the same idea.
    sms_template: str = ""
    sms_region: str = ""
    # Echo the code in the API answer while on the console channel. Development
    # only: it turns the endpoint into a way to log in as any phone number.
    sms_debug_echo: bool = False

    # Verification-code policy, shared by every channel. Kept separate from the
    # channel settings so adding mail did not have to clone the budget fields or
    # leave them under a name that says "sms" while they govern email too.
    verify_code_ttl: int = Field(default=300, ge=60, le=1800)
    # A second message to the same address within this window is refused;
    # without it a loop of requests becomes a way to spend the operator's
    # message budget and to annoy one person.
    verify_cooldown: int = Field(default=60, ge=0, le=3600)
    verify_daily_per_address: int = Field(default=10, ge=1, le=100)
    verify_daily_per_ip: int = Field(default=30, ge=1, le=1000)

    # Sign-up and sign-in by email. An address needs no template review to start
    # using and is reachable on a development machine through a local SMTP sink,
    # so this is the default door; SMS stays available behind it.
    mail_provider: str = "console"
    mail_debug_echo: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    # Defaults to SMTP_USER, which is what most providers require anyway.
    smtp_from: str = ""
    # Shown in the inbox list next to the address, e.g. "AgentForge". The address
    # itself still has to be the authenticated account on QQ and 163: they answer
    # 553 when the two differ, so a display name is the only part that is free.
    smtp_from_name: str = ""
    # starttls (587), ssl (465), none (a local sink such as Mailpit).
    smtp_tls: str = "starttls"
    smtp_timeout: float = Field(default=10, gt=0, le=60)

    # Whether an unknown address becomes a new account. Off means accounts are
    # created by an operator only, and the endpoint says the address is unknown.
    signup_open: bool = True

    @field_validator("sandbox_image")
    @classmethod
    def _known_image(cls, value: str) -> str:
        try:
            SandboxPolicy(image=value)
        except Exception as exc:
            raise ValueError(f"Unknown sandbox image {value!r}.") from exc
        return value
