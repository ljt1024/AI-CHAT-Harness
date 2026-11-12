"""Installed skills: list, install, uninstall."""
import logging

from fastapi import APIRouter, HTTPException

from agentforge.api.deps import AppDeps
from agentforge.skills import SkillPackage, remove_synced_skill

logger = logging.getLogger(__name__)


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    settings, store, pool = d.settings, d.store, d.pool
    ready, owner_id = d.ready, d.owner_id
    skill_manager = d.skill_manager

    @router.get("/api/skills")
    async def list_skills():
        ready()
        uid = owner_id()
        packages = await store.list("skills", owner=uid)
        return {"code": 200, "data": [{k: v for k, v in p.items() if k != "files"}
            | {"files": list(p["files"])} for p in packages]}

    @router.post("/api/skills", status_code=201)
    async def install_skill(package: SkillPackage):
        ready()
        owner_id()
        installed = await skill_manager.install(package)
        return {"code": 201, "data": {k: v for k, v in installed.items() if k != "files"}
            | {"files": list(installed["files"])}}

    @router.delete("/api/skills/{identity}")
    async def uninstall_skill(identity: str):
        ready()
        uid = owner_id()
        try:
            package = await skill_manager.delete(identity)
        except ValueError as exc:
            raise HTTPException(404 if "not installed" in str(exc) else 409, str(exc)) from None
        removed = 0
        # Only this account's workspaces. A sandbox workspace is named after the
        # container, not the session, so the session row is what decides
        # ownership; a skill id synced into someone else's conversation stays.
        roots = []
        for session in await store.list("sessions", owner=uid):
            lease = pool.workspaces.get(session["id"])
            roots.append(lease.root if lease is not None
                         else settings.agentforge_workspace / session["id"])
        for root in roots:
            try:
                removed += remove_synced_skill(root, identity)
            except (OSError, ValueError):
                logger.warning("Could not clean synced skill %s in %s", identity, root)
        return {"code": 200, "data": {"id": identity, "name": package["name"],
            "removed_workspaces": removed}}

    return router
