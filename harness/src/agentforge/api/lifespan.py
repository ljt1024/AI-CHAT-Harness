"""Startup and shutdown.

Split out of `create_app` because it is a sequence, not a wiring step: a reader
checking "what does boot actually do" should be able to read it top to bottom
without scrolling past forty route decorators.
"""
import asyncio
from contextlib import asynccontextmanager
import logging

from agentforge import secrets_box
from agentforge.api.deps import AppDeps
from agentforge.auth import current_user
from agentforge.models import DEFAULT_AGENT_ID, AgentDefinition

logger = logging.getLogger(__name__)


def build_lifespan(deps: AppDeps):
    settings, store = deps.settings, deps.store
    default = AgentDefinition(id="default", name="AgentForge",
        description="通用助手：可以读写工作区文件、运行受限命令、搜索并读取网页、查询商品价格、按需加载技能。",
        tools=["read_file", "write_file", "grep", "glob", "run_command", "ask_user",
               "web_search", "web_fetch", "search_products", "get_product"])

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        try:
            await store.setup()
            # The cipher has to exist before anything writes a token: the startup
            # catalogue below writes them, and rows written by an older version
            # are rewritten once, here.
            secrets_box.configure(settings.agentforge_workspace,
                                  settings.mcp_secret_key.get_secret_value())
            await deps.encrypt_stored_tokens()
            await deps.pool.start()
            await deps.auth.setup()
            await deps.auth.prune()
            await deps.auth.prune_guests(settings.guest_retention_hours)
            owner = await deps.auth.bootstrap(settings.admin_user,
                                              settings.admin_password.get_secret_value())
            # An installation can hold accounts while the environment stopped
            # naming one (a developer's machine, an operator who rotated the
            # file). Legacy rows still need an owner, or they stay invisible to
            # everyone -- the same failure as the built-in agent below.
            operator = owner["id"] if owner else await deps.auth.first_owner()
            if operator is None:
                logger.warning("No account exists yet. Set ADMIN_USER and ADMIN_PASSWORD, then "
                               "restart: every /api request is refused until then.")
            else:
                if owner is None:
                    logger.warning("ADMIN_USER is not configured; binding legacy resources to "
                                   "the first account (%s).", operator)
                # Resources written before accounts existed belong to the account
                # the operator designated, not to whoever logs in first. The
                # built-in agent is excluded there on purpose.
                await deps.auth.adopt_ownerless(operator)
            # The built-in agent is shared: its empty owner means every account,
            # visitor included, may read and run it, and nobody may replace it.
            # So exactly one row carries that id. An earlier migration bound it
            # to an account, which reads fine for that account and empties the
            # catalogue for everyone else, and no API path can create another
            # row with this id -- anything private under it is that leftover.
            orphaned = await store.db.agents.delete_many(
                {"id": DEFAULT_AGENT_ID, "owner_id": {"$ne": ""}})
            if orphaned.deleted_count:
                logger.warning("Dropped %d private copy of the built-in agent; it is "
                               "shared read-only.", orphaned.deleted_count)
            await store.put("agents", default.model_dump(), owner="")
            # MCP servers belong to an account, so a row with an empty owner is a
            # leftover from a boot that had no account yet -- the config file is
            # the only thing that plants them. Left alone it reads as shared, and
            # the next config refresh inserts an owned twin with the same id; the
            # list then carries the server twice, discovery runs twice, and every
            # save that validates an MCP-backed agent dies on "Duplicate tool".
            # Bind the only copy, drop the stale twin.
            if operator:
                shared = await store.db.mcp_servers.find({"owner_id": ""}).to_list(length=10000)
                for row in shared:
                    owned = await store.db.mcp_servers.count_documents(
                        {"id": row["id"], "owner_id": operator})
                    if owned:
                        await store.db.mcp_servers.delete_one({"_id": row["_id"]})
                    else:
                        await store.db.mcp_servers.update_one(
                            {"_id": row["_id"]}, {"$set": {"owner_id": operator}})
                if shared:
                    logger.info("Settled %d shared MCP rows onto %s", len(shared), operator)
            # The startup MCP file is planted by the operator, so its rows belong
            # to that account rather than to nobody in particular. Seeding under
            # the operator (not the configured `owner`, which can be absent while
            # accounts exist) is what stops the rows coming back ownerless.
            planted = current_user.set(operator) if operator else None
            try:
                await deps.load_mcp_config(settings.mcp_config)
                await deps.load_mcp_catalog(settings.mcp_catalog)
            finally:
                if planted is not None:
                    current_user.reset(planted)
            app.state.ready = True
        except Exception:
            logger.warning("MongoDB unavailable; data endpoints return 503.", exc_info=True)
        yield
        for _, task in deps.runs.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in deps.runs.values()), return_exceptions=True)
        await deps.pool.close()

    return lifespan
