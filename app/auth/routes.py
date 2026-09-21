"""Authentication routes + FastAPI session dependencies."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, field_validator

from ..config import Settings
from ..database.repositories import Repos
from ..utils import secure_cookie_params
from .service import AuthError, AuthService

log = logging.getLogger("bot_indexer.auth")

SESSION_COOKIE = "session"

# ---------------------------------------------------------------------------
# Login rate limiting (in-memory sliding window per IP)
# ---------------------------------------------------------------------------

_login_attempts: dict[str, deque] = {}


def _login_limited(ip: str, limit: int = 5, window: int = 300) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    dq = _login_attempts.setdefault(ip, deque())
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def get_repos(request: Request) -> Repos:
    return request.app.state.repos


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def _bearer_token(request: Request) -> str | None:
    h = request.headers.get("authorization") or ""
    if h.startswith("Bearer "):
        return h[7:].strip() or None
    return None


async def get_session_user(request: Request) -> dict | None:
    auth: AuthService = request.app.state.auth
    repos = request.app.state.repos

    # 1) Bearer token — the cookie-less path for embedded previews whose
    #    browsers block third-party cookies (the JS stores the session token
    #    in its own (partitioned) localStorage and sends it on every call).
    token = _bearer_token(request)
    if token:
        session = await auth.get_session(token)
        if session:
            user = await repos.users.get(session["user_id"])
            if user and user.get("status") == "ACTIVE":
                request.state.session_user = user
                return user
        # a bearer that does not resolve must not fall through to other
        # credentials (prevents mixing/stale-token confusion)
        if request.url.path.startswith("/api/"):
            return None

    # 2) Session cookie (normal browser contexts)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = await auth.get_session(token)
        if session:
            user = await repos.users.get(session["user_id"])
            if user and user.get("status") == "ACTIVE":
                request.state.session_user = user
                return user

    # 3) One-time handoff token in the query string (page loads in
    #    cookie-less embeds; single use, 60 s TTL)
    st = request.query_params.get("st")
    if st and request.method == "GET" and not request.url.path.startswith("/api/"):
        grant = await auth.consume_handoff(st)
        if grant:
            user_id, session_token = grant
            user = await repos.users.get(user_id)
            if user and user.get("status") == "ACTIVE":
                # Only this authenticated, non-cacheable page receives its own
                # session bootstrap. No reusable credential goes in the URL.
                request.state.bootstrap_session = session_token
                request.state.session_user = user
                return user
    return None


async def require_user(
    request: Request,
    user: dict | None = Depends(get_session_user),
) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


async def require_user_page(
    request: Request,
    user: dict | None = Depends(get_session_user),
) -> dict:
    if user is None:
        raise HTTPException(
            status_code=302, headers={"Location": "/login?next=" + request.url.path}
        )
    return user


def require_admin_page(user: dict = Depends(require_user_page)) -> dict:
    if user.get("role") != "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


# ---------------------------------------------------------------------------
# Public helpers for other routes
# ---------------------------------------------------------------------------


def set_session_cookie(
    response: Response,
    token: str,
    lifetime_hours: int,
    settings: Settings,
    request: Request | None = None,
) -> None:
    scheme = request.url.scheme if request is not None else "http"
    xproto = request.headers.get("x-forwarded-proto") if request is not None else None
    host = request.url.netloc if request is not None else ""
    secure, samesite = secure_cookie_params(scheme, xproto, host, settings)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime_hours * 3600),
        httponly=True,
        samesite=samesite,
        secure=secure,
        path="/",
    )


def clear_session_cookie(
    response: Response, request: Request | None = None, settings: Settings | None = None
) -> None:
    secure = False
    samesite = "lax"
    if request is not None and settings is not None:
        scheme = request.url.scheme
        xproto = request.headers.get("x-forwarded-proto")
        host = request.url.netloc
        secure, samesite = secure_cookie_params(scheme, xproto, host, settings)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite=samesite, secure=secure
    )


def public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        if not v:
            raise ValueError("Password is required.")
        return v


@router.post("/login")
async def api_login(
    payload: LoginIn, request: Request, response: Response
) -> dict:
    auth: AuthService = request.app.state.auth
    settings: Settings = request.app.state.settings
    ip = request.client.host if request.client else "unknown"
    if not _login_limited(
        ip, limit=settings.login_rate_limit, window=settings.login_rate_window_seconds
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in a few minutes.",
        )
    try:
        user = await auth.authenticate(payload.email, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    token, expires_at = await auth.create_session(user["id"])
    await request.app.state.repos.events.add(
        "USER_LOGIN",
        f"User '{user['email']}' logged in",
        user_id=user["id"],
        status="SUCCESS",
    )
    log.info("user_id=%s event=USER_LOGIN", user["id"])

    set_session_cookie(
        response, token, settings.session_lifetime_hours, settings, request
    )
    # ``token`` is also returned so cookie-less embedded contexts (browsers
    # that block third-party cookies) can keep the session in localStorage
    # and send it as ``Authorization: Bearer``.
    return {"user": public_user(user), "expires_at": expires_at, "token": token}


@router.post("/handoff")
async def api_handoff(
    request: Request, user: dict = Depends(require_user)
) -> dict:
    """Mint a one-time, 60-second token that authenticates a single page
    load via ``?st=`` (used by cookie-less embedded previews)."""
    token = _bearer_token(request) or request.cookies.get(SESSION_COOKIE)
    return {"st": request.app.state.auth.create_handoff(user["id"], token)}


@router.post("/logout")
async def api_logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    bearer = _bearer_token(request)
    user = await get_session_user(request)
    await request.app.state.auth.destroy_session(token)
    if bearer:
        await request.app.state.auth.destroy_session(bearer)
    if user:
        await request.app.state.repos.events.add(
            "USER_LOGOUT",
            f"User '{user['email']}' logged out",
            user_id=user["id"],
            status="SUCCESS",
        )
    clear_session_cookie(response, request, request.app.state.settings)
    return {"ok": True}


@router.get("/me")
async def api_me(user: dict = Depends(require_user)) -> dict:
    return {"user": public_user(user)}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

pages = APIRouter(tags=["auth-pages"])


@pages.get("/login", include_in_schema=False)
async def login_page(request: Request):
    existing = await get_session_user(request)
    if existing:
        return RedirectResponse("/dashboard", status_code=302)
    from .. import templates

    return templates.render(
        request,
        "login.html",
        {
            "app_name": request.app.state.settings.app_name,
        },
    )


@pages.get("/logout", include_in_schema=False)
async def logout_page(request: Request, response: Response) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    bearer = _bearer_token(request)
    await request.app.state.auth.destroy_session(token)
    if bearer:
        await request.app.state.auth.destroy_session(bearer)
    clear_session_cookie(response, request, request.app.state.settings)
    return RedirectResponse("/login", status_code=302)
