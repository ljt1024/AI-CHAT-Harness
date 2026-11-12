"""The harness HTTP surface.

`agentforge.api.create_app` is the entry point (`server.py` calls it). The
package below splits what used to be one 1,873-line module:

- `app`       -- construction: store, gateway, pool, middlewares, routers
- `deps`      -- the shared state and the services built on it
- `lifespan`  -- startup and shutdown
- `schemas`   -- request and response bodies
- `events`    -- the legacy SSE envelope
- `routes/`   -- one module per resource, each a `build_router(deps)`

`legacy_event` and `LEGACY_EVENT_NAMES` stay exported from here because
`test_event_contract` checks the wire mapping against them, and `Runtime` because
tests patch its `run` method to resume from a checkpoint.
"""
from agentforge.api.app import create_app
from agentforge.api.events import (LEGACY_EVENT_NAMES, event_frames, legacy_event,
                                       legacy_result)
from agentforge.loop import Runtime

__all__ = ["create_app", "LEGACY_EVENT_NAMES", "legacy_event", "legacy_result",
           "event_frames", "Runtime"]
