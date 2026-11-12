"""One-shot execution in a disposable sandbox.

Host tooling (tests, verification scripts) needs the agent's isolation without a
model turn, so this endpoint borrows the same pool, seccomp profile and network
policy rather than running untrusted code on the host.
"""
import asyncio

from fastapi import APIRouter, HTTPException

from agentforge.api.deps import AppDeps
from agentforge.api.schemas import SandboxExecRequest
from agentforge.models import new_id
from agentforge.sandbox.docker_runner import SandboxUnavailable


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    pool = d.pool
    owner_id = d.owner_id

    @router.post("/api/sandbox/exec")
    async def sandbox_exec(request: SandboxExecRequest):
        # Runs host-supplied files and a command inside a disposable offline
        # container. This is the escape hatch that keeps skill scripts and other
        # untrusted code off the host: the same pool, seccomp profile and network
        # isolation the agent uses, without going through a model turn.
        session_id = new_id()
        lease = None
        try:
            lease = await pool.acquire(session_id, request.image, owner=owner_id())
            for item in request.files:
                relative = item.path
                content = item.content
                if relative.startswith("/") or ".." in relative.split("/"):
                    raise HTTPException(422, f"Unsafe file path: {relative}")
                try:
                    target = lease.sandbox.workspace.path(relative)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_text, content, encoding="utf-8")
            result = await pool.runner.execute(lease.sandbox, request.command, request.timeout)
            return {"code": 200, "data": {"ok": result.ok, "output": result.output, "error": result.error}}
        except SandboxUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        finally:
            if lease is not None:
                await pool.release(session_id)

    return router
