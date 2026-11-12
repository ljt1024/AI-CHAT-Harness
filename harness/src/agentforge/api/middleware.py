"""The two HTTP middlewares: who is calling, and what happened.

Order matters and is fixed in `app.create_app`: `authenticate` is registered
first, `log_request` second. Starlette wraps in reverse, so logging stays
outermost and a 401 still carries `X-Request-Id` -- which is the whole point of
having one.
"""
import logging
import re
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from agentforge.api.deps import AppDeps
from agentforge.auth import (GUEST_COOKIE, SESSION_COOKIE, current_user, new_guest_id,
                             valid_guest_id)
from agentforge.logging_config import request_id
from agentforge.models import new_id

logger = logging.getLogger(__name__)

# The only route that receives a file body. Bounding it in the HTTP middleware
# matters because Starlette spools the whole multipart form before the endpoint
# runs, so a limit applied inside the endpoint is already too late.
UPLOAD_PATH = re.compile(r"^/api/sessions/[^/]+/files$")

# The only routes reachable without a session. Everything else under /api is
# refused before it reaches a handler, so a new endpoint is protected by
# default instead of by remembering to add a check.
PUBLIC_API = {"/api/auth/login", "/api/auth/logout",
              "/api/auth/sms/code", "/api/auth/sms/verify",
              "/api/auth/email/code", "/api/auth/email/verify"}


def build_authenticate(deps: AppDeps):
    async def authenticate(request, call_next):
        path = request.url.path
        # Static assets are served by nginx or Vite; only the API is ours to guard.
        if not path.startswith("/api"):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        try:
            user = await deps.auth.user_for_token(token) if token else None
        except Exception:
            logger.warning("Session lookup failed; treating the request as anonymous.",
                           exc_info=True)
            user = None

        # A visitor without a session is not turned away at the door: it is given
        # an identity of its own so the ownership filters work unchanged, and only
        # the routes in GUEST_ROUTES are reachable through it.
        guest, issued = "", ""
        if user is None and path not in PUBLIC_API:
            permitted = path == "/api/auth/me" or (
                deps.settings.guest_turns > 0 and deps.guest_allowed(request.method, path))
            if not permitted:
                return JSONResponse({"detail": "Not authenticated.",
                                     "code": "login_required"}, status_code=401)
            candidate = request.cookies.get(GUEST_COOKIE, "")
            guest = candidate if valid_guest_id(candidate) else new_guest_id()
            issued = "" if valid_guest_id(candidate) else guest

        identity = user["id"] if user else guest
        claim = current_user.set(identity) if identity else None
        try:
            # Applies to every unsafe method, signed in or not: a form posted
            # from another site would otherwise be able to sign a visitor into
            # an account the attacker owns (login CSRF), which a check gated on
            # "has an identity already" cannot see.
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not deps.same_origin(request):
                return JSONResponse({"detail": "Cross-origin request rejected."},
                                    status_code=403)
            # An upload is the one body worth bounding before it is read:
            # Starlette spools the whole multipart form to disk before the
            # handler runs, so a check inside the endpoint would already have
            # paid for the bytes. Answers the same 413 the endpoint would.
            if request.method == "POST" and UPLOAD_PATH.fullmatch(path):
                declared = request.headers.get("content-length", "")
                limit = deps.settings.upload_max_bytes
                if declared.isdigit() and int(declared) > limit + 65_536:
                    return JSONResponse(
                        {"detail": f"File exceeds the {limit // (1024 * 1024)} MB limit."},
                        status_code=413)
            if user is not None:
                request.state.user = user
            if guest:
                request.state.guest = guest
            response = await call_next(request)
        finally:
            if claim is not None:
                current_user.reset(claim)
        if issued:
            # A week is long enough to be worth keeping and short enough that a
            # stale anonymous transcript is not defended forever; the daily
            # counter resets on its own, but the transcript does not.
            response.set_cookie(GUEST_COOKIE, issued, max_age=7 * 24 * 3600,
                                httponly=True, samesite="lax",
                                secure=deps.settings.cookie_secure, path="/")
        return response
    return authenticate


async def log_request(request: Request, call_next):
    # The id is echoed in the response header so a user reporting a problem
    # can quote one value and have every related record filtered out. It is
    # honoured when a caller supplies its own, so a browser trace and the
    # server log can share it.
    supplied = request.headers.get("X-Request-Id", "").strip()
    rid = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else new_id()[:12]
    token = request_id.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s failed after %.0fms", request.method, request.url.path,
                         (time.perf_counter() - started) * 1000)
        raise
    else:
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = rid
        # Health is polled constantly and says nothing on success.
        emit_log = logger.debug if request.url.path == "/health" else logger.info
        emit_log("%s %s -> %s %.0fms", request.method, request.url.path,
                 response.status_code, elapsed)
        return response
    finally:
        request_id.reset(token)
