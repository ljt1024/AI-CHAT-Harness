"""Agent definitions: list, create, update, delete, and the tool catalogue."""
from fastapi import APIRouter, HTTPException

from agentforge.api.deps import AppDeps
from agentforge.models import DEFAULT_AGENT_ID, AgentDefinition
from agentforge.skills import SKILL_TOOL_NAMES


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    store = d.store
    ready, owner_id = d.ready, d.owner_id
    tools_factory, validate_definition = d.tools_factory, d.validate_definition

    @router.get("/api/agents")
    async def agents():
        ready()
        listed = await store.list("agents", owner=owner_id())
        return {"code": 200, "data": [{**a, "modelId": a["model"]["model_id"],
            "systemPrompt": a["system_prompt"]} for a in listed]}

    @router.get("/api/tools")
    async def tools_catalog():
        """The tools an agent definition may select.

        Kept separate from the agent rows because the wizard has to offer the
        real catalogue: a hardcoded list in the browser drifts the moment a tool
        is added, and the create endpoint rejects the whole definition over one
        unknown name. Skill tools are left out on purpose — `prepare_agent`
        appends them to every agent, so they are not a choice.
        """
        ready()
        registry = tools_factory("catalog")
        return {"code": 200, "data": [
            {"name": tool.name, "description": tool.description,
             "permission": tool.permission, "source": tool.source}
            for name, tool in registry.tools.items() if name not in SKILL_TOOL_NAMES]}

    @router.post("/api/agents", status_code=201)
    async def create_agent(agent: AgentDefinition):
        ready()
        uid = owner_id()
        if agent.id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent id is reserved.")
        await validate_definition(agent)
        if await store.get("agents", agent.id, owner=uid):
            raise HTTPException(409, "Agent ID already exists.")
        return {"code": 201, "data": await store.put("agents", agent.model_dump(), owner=uid)}

    @router.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, agent: AgentDefinition):
        ready()
        uid = owner_id()
        # The built-in row is shared read-only: every account runs it, so no
        # account may rewrite what the others see.
        if agent_id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent is shared and cannot be edited.")
        if await store.get("agents", agent_id, owner=uid) is None:
            raise HTTPException(404, "Agent not found.")
        # The path decides the identity: a body carrying another id must not be
        # able to rename this row over somebody else's (or its own previous id).
        agent = agent.model_copy(update={"id": agent_id})
        await validate_definition(agent)
        return {"code": 200, "data": await store.put("agents", agent.model_dump(), owner=uid)}

    @router.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str):
        ready()
        if agent_id == DEFAULT_AGENT_ID:
            raise HTTPException(409, "The built-in agent is shared and cannot be deleted.")
        result = await store.db.agents.delete_one({"id": agent_id, "owner_id": owner_id()})
        if not result.deleted_count:
            raise HTTPException(404, "Agent not found.")
        return {"code": 200, "data": {"id": agent_id}}

    return router
