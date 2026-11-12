"""MCP registration, the operator's catalogue, and installing from it."""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from agentforge.api.deps import AppDeps
from agentforge.api.schemas import MCPConfig, MCPToggle
from agentforge.capabilities import MCPServerRequest, parse_config, public_server
from agentforge.catalog import InstallRequest as CatalogInstall, public_item
from agentforge.auth import is_guest

logger = logging.getLogger(__name__)


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    store = d.store
    ready, owner_id = d.ready, d.owner_id
    save_server = d.save_server

    @router.get("/api/mcp/servers")
    async def list_mcp_servers():
        ready()
        owner_id()
        servers = await store.list("mcp_servers")
        return {"code": 200, "data": [public_server(server) for server in servers]}

    @router.post("/api/mcp/servers", status_code=201)
    async def register_mcp_server(request: MCPServerRequest):
        ready()
        owner_id()
        if await store.get("mcp_servers", request.id):
            raise HTTPException(409, "MCP server ID already exists.")
        return {"code": 201, "data": await save_server(request)}

    @router.post("/api/mcp/servers/import", status_code=201)
    async def import_mcp_server(payload: MCPConfig):
        """Accept the `mcpServers` block a catalogue gives you, verbatim."""
        ready()
        owner_id()
        try:
            request = parse_config(payload.config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # Re-pasting the same config with a new token refreshes that row: the
        # URL identifies the service, so a second import must not duplicate it.
        existing = next((server for server in await store.list("mcp_servers")
                         if server["url"] == request.url), None)
        if existing:
            request.id = existing["id"]
        else:
            base, suffix = request.id, 1
            while await store.get("mcp_servers", request.id):
                suffix += 1
                request.id = f"{base[:18].rstrip('_')}_{suffix}"
        return {"code": 201, "data": await save_server(request)}

    @router.get("/api/mcp/catalog")
    async def list_mcp_catalog():
        """What this deployment offers, with the rows already installed marked."""
        ready()
        owner_id()
        installed = {server["id"] for server in await store.list("mcp_servers")}
        items = await store.list("mcp_catalog")
        return {"code": 200,
                "data": [public_item(item, installed) for item in sorted(items, key=lambda i: i["id"])]}

    @router.post("/api/mcp/catalog/{identity}/install", status_code=201)
    async def install_from_catalog(identity: str, payload: CatalogInstall):
        """Install a server the operator chose.

        The request carries a credential and nothing else. The URL comes from the
        catalogue row, which is the whole reason this endpoint exists: an MCP
        registration makes the host dial an address, so the address has to be one
        the operator picked rather than one a user typed into a page.
        """
        ready()
        if is_guest(owner_id()):
            raise HTTPException(401, "An account is required to install a server.")
        item = await store.get("mcp_catalog", identity)
        if item is None or item.get("owner_id") != "":
            raise HTTPException(404, "No such catalogue entry.")
        if not item.get("enabled", True):
            raise HTTPException(409, "This catalogue entry is switched off.")
        if await store.get("mcp_servers", identity):
            raise HTTPException(409, "This server is already installed.")
        token = (payload.token or "").strip() or None
        if item.get("auth") == "token" and not token:
            raise HTTPException(422, "This service needs an access token.")
        try:
            request = MCPServerRequest(id=identity, url=item["url"], token=token)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()[0]["msg"].removeprefix("Value error, ")) from None
        return {"code": 201,
                "data": await save_server(request, allow_private=bool(item.get("private")))}

    @router.delete("/api/mcp/servers/{identity}")
    async def remove_mcp_server(identity: str):
        ready()
        owner_id()
        if await store.get("mcp_servers", identity) is None:
            raise HTTPException(404, "MCP server is not registered.")
        if await store.list("agents", {"mcp_servers": identity}):
            raise HTTPException(409, "MCP server is referenced by a saved agent.")
        await store.db.mcp_servers.delete_one({"id": identity, **store.scope()})
        logger.info("mcp server removed id=%s", identity)
        return {"code": 200}

    @router.patch("/api/mcp/servers/{identity}")
    async def toggle_mcp_server(identity: str, payload: MCPToggle):
        """Switch a server off without forgetting its URL and token.

        Deleting is the wrong tool here: a hosted service that is temporarily
        broken would have to be registered again from scratch, token included.
        """
        ready()
        owner_id()
        server = await store.get("mcp_servers", identity)
        if server is None:
            raise HTTPException(404, "MCP server is not registered.")
        await store.put("mcp_servers", {**server, "enabled": payload.enabled})
        logger.info("mcp server id=%s enabled=%s", identity, payload.enabled)
        return {"code": 200, "data": public_server({**server, "enabled": payload.enabled})}

    return router
