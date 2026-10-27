import asyncio
import json
import re
from urllib.parse import urlsplit

from pydantic import (BaseModel, Field, ValidationError, field_validator,
                      model_validator)

from agentforge.mcp_client import discover_tools
from agentforge.redact import mask_url

DISCOVER_TIMEOUT = 30.0
MAX_CONFIG = 20_000
# The names clients use for the transport we speak. Everything else (sse,
# stdio) is a different protocol and must not be silently downgraded.
_TRANSPORTS = {"streamable_http", "streamablehttp", "http"}
_ID = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


def clean_token(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    # Anything else would be mangled by an HTTP header parser.
    if not re.fullmatch(r"[\x21-\x7e]{8,512}", value):
        raise ValueError("Use 8..512 printable ASCII characters and no spaces.")
    return value


class MCPServerRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    url: str = Field(max_length=2000)
    # Bearer token for services that require one (ModelScope deployments made
    # with auth_check). Optional: many deployments treat the URL as the secret.
    token: str | None = Field(default=None, max_length=512, repr=False)
    # Only present so a config copied from a catalogue can be posted verbatim;
    # both are folded into `token` and never stored.
    type: str | None = None
    headers: dict[str, str] | None = Field(default=None, max_length=8, repr=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment or parsed.query):
            raise ValueError("Use an HTTP(S) MCP URL without credentials, query or fragment.")
        return url

    @field_validator("token")
    @classmethod
    def validate_token(cls, token):
        return clean_token(token)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value):
        if value is None:
            return None
        name = value.strip().lower().replace("-", "_")
        if name not in _TRANSPORTS:
            raise ValueError(f"Only Streamable HTTP is supported, not {value!r}.")
        return "streamable_http"

    @model_validator(mode="after")
    def fold_headers(self):
        """Keep one credential, from whichever field the config used.

        A pasted config carries `Authorization: Bearer ...` while the runtime
        only ever sends that one header, so anything else is refused instead of
        being dropped in silence.
        """
        token = self.token
        for name, value in (self.headers or {}).items():
            if name.strip().lower() != "authorization":
                raise ValueError(f"Only an Authorization header is supported, not {name!r}.")
            scheme, _, credential = value.strip().partition(" ")
            if scheme.lower() != "bearer" or not credential.strip():
                raise ValueError("Authorization must look like 'Bearer <token>'.")
            credential = credential.strip()
            if token and token != credential:
                raise ValueError("The token and the Authorization header disagree.")
            token = credential
        self.token = clean_token(token)
        self.headers = None
        return self


def derive_id(name: str | None) -> str:
    """The catalogue key is the name the user gave the server, so keep it.

    A bare server object has no key; "mcp" plus the caller's collision suffix
    is more useful than a guess invented from the hostname.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:20].rstrip("_")
    return slug if _ID.match(slug) else "mcp"


def _reason(exc: ValidationError) -> str:
    """The one sentence a user needs, not pydantic's whole report."""
    return exc.errors()[0]["msg"].removeprefix("Value error, ")


def parse_config(text: str) -> MCPServerRequest:
    """Read the `mcpServers` block that MCP catalogues hand out.

    ModelScope, Smithery and the desktop clients all publish the same shape,
    so pasting that block is the path people actually take. One server per
    paste keeps the failure message attachable to a single row.
    """
    text = text.strip()
    if not text:
        raise ValueError("Paste an MCP configuration first.")
    if len(text) > MAX_CONFIG:
        raise ValueError("MCP configuration is too large.")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from None
    if not isinstance(document, dict):
        raise ValueError("The configuration must be a JSON object.")
    name, spec = None, document
    if "mcpServers" in document:
        servers = document["mcpServers"]
        if not isinstance(servers, dict) or not servers:
            raise ValueError("mcpServers must be a non-empty object.")
        if len(servers) > 1:
            raise ValueError("This configuration holds several servers; paste one at a time.")
        name, spec = next(iter(servers.items()))
    if not isinstance(spec, dict):
        raise ValueError("A server entry must be an object.")
    if "command" in spec:
        raise ValueError("A command-launched (stdio) server cannot run here; use a Streamable "
                         "HTTP URL.")
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("The server entry needs a url.")
    headers = spec.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise ValueError("headers must be an object.")
    try:
        return MCPServerRequest(
            id=derive_id(name), url=url.strip(),
            token=spec.get("token") if isinstance(spec.get("token"), str) else None,
            type=spec.get("type") if isinstance(spec.get("type"), str) else None,
            headers={str(key): str(value) for key, value in headers.items()} if headers else None)
    except ValidationError as exc:
        raise ValueError(_reason(exc)) from None


def public_server(server: dict) -> dict:
    """The shape the browser is allowed to see: no token, no raw URL.

    A hosted deployment URL contains the tenant identifier, which is the whole
    credential when the service does not require a token. The UI only needs to
    recognise the row, so the opaque segment is masked.
    """
    return {"id": server["id"], "url": mask_url(server["url"]),
            "tools": server.get("tools", []), "hasToken": bool(server.get("token"))}


async def server_tools(server, discover_timeout: float | None = None,
                       call_timeout: float | None = None):
    try:
        async with asyncio.timeout(discover_timeout or DISCOVER_TIMEOUT):
            tools = await discover_tools(server["url"], server.get("token"), call_timeout)
    except TimeoutError:
        raise ValueError("MCP server did not answer the tool listing in time.") from None
    if len(tools) > 64:
        raise ValueError("MCP server exceeds the 64-tool limit.")
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError("MCP server returned duplicate tool names.")
    for tool in tools:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,38}", tool.name):
            raise ValueError("MCP tool names must use 1..38 letters, digits, underscores or hyphens.")
        tool.remote_name = tool.name
        tool.name = f"mcp_{server['id']}__{tool.name}"
        tool.description = f"External MCP service ({server['id']}): {tool.description}"
        tool.permission = "confirm"
    return tools
