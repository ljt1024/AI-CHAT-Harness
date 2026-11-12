"""Liveness and the model catalogue the browser offers."""
import asyncio

from fastapi import APIRouter

from agentforge.api.deps import AppDeps


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    settings, gateway, pool = d.settings, d.gateway, d.pool
    app, runs = d.app, d.runs

    @router.get("/health")
    async def health():
        # Probe the three things a run actually depends on. Reporting "ok" on the
        # database alone hid a missing model key or a dead Docker daemon until a
        # run failed, which made the endpoint useless for triage.
        adapter = gateway.adapters.get("deepseek")
        model_ready = bool(getattr(adapter, "api_key", None))
        try:
            async with asyncio.timeout(3):
                await pool.runner.connect()
            docker_ready = True
        except Exception:
            docker_ready = False
        return {"status": "ok" if app.state.ready and model_ready else "degraded",
                "database": app.state.ready, "model_key": model_ready, "docker": docker_ready,
                "sandbox": pool.status(), "active_runs": len(runs), "mode": "runtime"}

    @router.get("/api/models")
    async def models():
        return {"code": 200, "data": [{"id": "deepseek-chat", "name": "DeepSeek Chat",
            "provider": "deepseek", "supportsStream": True, "supportsThinking": False,
            "supportsTools": True, "supportsFileUpload": False,
            "enabled": bool(settings.deepseek_api_key.get_secret_value())}]}

    return router
