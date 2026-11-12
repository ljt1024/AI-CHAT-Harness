"""Sign-in doors: password, SMS code, email code, binding, logout, whoami."""
import logging

from fastapi import APIRouter, HTTPException, Request, Response

from agentforge import email_address
from agentforge import phone as phone_number
from agentforge.api.deps import AppDeps
from agentforge.api.schemas import (EmailBindRequest, EmailCodeRequest, EmailLoginRequest,
                                    LoginRequest, PhoneBindRequest, SmsCodeRequest,
                                    SmsLoginRequest)
from agentforge.auth import SESSION_COOKIE, AuthError, SmsCodeError

logger = logging.getLogger(__name__)


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    settings, auth, sms, mail = d.settings, d.auth, d.sms, d.mail
    require_ready, public_user = d.require_ready, d.public_user
    finish_login, client_ip = d.finish_login, d.client_ip

    @router.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response):
        require_ready()
        try:
            user = await auth.authenticate(payload.username, payload.password)
        except AuthError as exc:
            logger.info("login failed user=%s", payload.username[:32])
            raise HTTPException(401, str(exc)) from None
        logger.info("login user=%s", user["username"])
        return await finish_login(user, request, response)

    @router.post("/api/auth/sms/code")
    async def send_code(payload: SmsCodeRequest, request: Request):
        """Send a sign-in code, or a binding code to a signed-in account.

        Registration and sign-in share one endpoint because they are one flow:
        whether this number is new is not something the sender should have to
        know before asking, and telling it here would turn the endpoint into a
        way to test whether somebody has an account.
        """
        require_ready()
        try:
            number = phone_number.normalize(payload.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        user = getattr(request.state, "user", None)
        if payload.purpose == "bind":
            if user is None:
                raise HTTPException(401, detail={"code": "login_required"})
            bound = await auth.user_by_phone(number)
            if bound and bound["id"] != user["id"]:
                raise HTTPException(409, detail={"code": "phone_taken"})
        else:
            # Do not spend a message on a number that cannot become an account.
            if not settings.signup_open and await auth.user_by_phone(number) is None:
                raise HTTPException(403, detail={"code": "signup_closed"})
        if sms is None:
            raise HTTPException(503, detail={"code": "sms_unavailable"})
        try:
            sent = await auth.issue_code(number, payload.purpose, client_ip(request), sms,
                                         channel="sms")
        except SmsCodeError as exc:
            status = 429 if exc.reason in {"cooldown", "daily_limit"} else 502
            raise HTTPException(status, detail={"code": exc.reason,
                                               "retryAfter": exc.retry_after}) from None
        data = {"expiresIn": sent["expiresIn"], "cooldown": sent["cooldown"]}
        if getattr(sms, "echo", False):
            # Console channel with echo on: lets a tester finish signing in
            # without a phone. `build_sender` refuses to do this quietly.
            data["debugCode"] = sent["code"]
        logger.info("sms code sent phone=%s purpose=%s", phone_number.mask(number),
                    payload.purpose)
        return {"code": 200, "data": data}

    @router.post("/api/auth/sms/verify")
    async def verify_sms(payload: SmsLoginRequest, request: Request, response: Response):
        """Sign in with a code; an unknown number becomes a new account."""
        require_ready()
        try:
            number = await auth.verify_code(payload.phone, "login", payload.code,
                                            channel="sms")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        user = await auth.user_by_phone(number)
        if user is None:
            if not settings.signup_open:
                raise HTTPException(403, detail={"code": "signup_closed"})
            user = await auth.create_phone_user(number)
        logger.info("login by phone=%s", phone_number.mask(number))
        return await finish_login(user, request, response)

    @router.post("/api/auth/phone")
    async def bind_phone(payload: PhoneBindRequest, request: Request):
        """Attach a number to an existing account (the one `bootstrap()` made)."""
        require_ready()
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, detail={"code": "login_required"})
        try:
            number = await auth.verify_code(payload.phone, "bind", payload.code,
                                            channel="sms")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        try:
            updated = await auth.bind_phone(user["id"], number)
        except SmsCodeError as exc:
            raise HTTPException(409, detail={"code": exc.reason}) from None
        return {"code": 200, "data": public_user(updated)}

    @router.post("/api/auth/email/code")
    async def send_email_code(payload: EmailCodeRequest, request: Request):
        """Send a sign-in code, or a binding code to a signed-in account.

        Same shape as the SMS door, on purpose: registration and sign-in are one
        flow, and asking "is this address new" before sending would turn the
        endpoint into an account-enumeration oracle.
        """
        require_ready()
        try:
            address = email_address.normalize(payload.email)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        user = getattr(request.state, "user", None)
        if payload.purpose == "bind":
            if user is None:
                raise HTTPException(401, detail={"code": "login_required"})
            bound = await auth.user_by_email(address)
            if bound and bound["id"] != user["id"]:
                raise HTTPException(409, detail={"code": "email_taken"})
        else:
            # Do not spend a message on an address that cannot become an account.
            if not settings.signup_open and await auth.user_by_email(address) is None:
                raise HTTPException(403, detail={"code": "signup_closed"})
        if mail is None:
            raise HTTPException(503, detail={"code": "mail_unavailable"})
        try:
            sent = await auth.issue_code(address, payload.purpose, client_ip(request),
                                         mail, channel="email")
        except SmsCodeError as exc:
            status = 429 if exc.reason in {"cooldown", "daily_limit"} else 502
            raise HTTPException(status, detail={"code": exc.reason,
                                               "retryAfter": exc.retry_after}) from None
        data = {"expiresIn": sent["expiresIn"], "cooldown": sent["cooldown"]}
        if getattr(mail, "echo", False):
            # Console channel with echo on: lets a tester finish signing in
            # without a mailbox. `build_mailer` refuses to do this quietly.
            data["debugCode"] = sent["code"]
        logger.info("email code sent address=%s purpose=%s",
                    email_address.mask(address), payload.purpose)
        return {"code": 200, "data": data}

    @router.post("/api/auth/email/verify")
    async def verify_email_code(payload: EmailLoginRequest, request: Request,
                                response: Response):
        """Sign in with a code; an unknown address becomes a new account."""
        require_ready()
        try:
            address = await auth.verify_code(payload.email, "login", payload.code,
                                            channel="email")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        user = await auth.user_by_email(address)
        if user is None:
            if not settings.signup_open:
                raise HTTPException(403, detail={"code": "signup_closed"})
            user = await auth.create_email_user(address)
        logger.info("login by email=%s", email_address.mask(address))
        return await finish_login(user, request, response)

    @router.post("/api/auth/email")
    async def bind_email(payload: EmailBindRequest, request: Request):
        """Attach an address to an existing account (the one `bootstrap()` made)."""
        require_ready()
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, detail={"code": "login_required"})
        try:
            address = await auth.verify_code(payload.email, "bind", payload.code,
                                             channel="email")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SmsCodeError as exc:
            raise HTTPException(400, detail={"code": exc.reason}) from None
        try:
            updated = await auth.bind_email(user["id"], address)
        except SmsCodeError as exc:
            raise HTTPException(409, detail={"code": exc.reason}) from None
        return {"code": 200, "data": public_user(updated)}

    @router.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        await auth.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"code": 200, "data": {"loggedOut": True}}

    @router.get("/api/auth/me")
    async def whoami(request: Request):
        """Who is calling, plus how much trial is left.

        Always public: a page has to be able to ask "am I signed in" before it
        can decide anything, and the anonymous budget has to be readable without
        spending a turn.
        """
        user = getattr(request.state, "user", None)
        if user is not None:
            return {"code": 200, "data": public_user(user),
                    "guest": {"allowed": False, "turnsLeft": 0, "turnsTotal": 0}}
        guest = getattr(request.state, "guest", "")
        try:
            budget = await auth.guest_budget(guest, settings.guest_turns,
                                             settings.guest_daily_turns)
        except Exception:
            logger.warning("Guest budget lookup failed.", exc_info=True)
            budget = {"allowed": False, "turnsLeft": 0, "turnsTotal": 0}
        return {"code": 200, "data": None, "guest": budget}

    return router
