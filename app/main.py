"""BOT INDEXER — application entry point.

Run (development):
    python run.py
or:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.errors import ServerErrorMiddleware  # noqa: F401
from starlette.requests import ClientDisconnect  # noqa: F401

from . import __version__, templates
from .auth.routes import (
    get_session_user,
    pages as auth_pages,
    require_admin,
    require_admin_page,
    require_user,
    require_user_page,
    router as auth_router,
)
from .auth.service import AuthService, bootstrap_admin_if_needed
from .config import get_settings, validate_production
from .database.feather_store import Database
from .database.repositories import RUNTIME_SETTING_KEYS, Repos, effective_setting
from .integrations.google_search_console import GSCConfigError, GoogleSearchConsole
from .logging_setup import RingLogHandler, setup_logging
from .pdf.fetcher import HostRateLimiter, SafeFetcher
from .publishing.pages import rebase_page_urls
from .queue.manager import QueueManager
from .users.service import UserService
from .utils import secure_cookie_params, utcnow, utcnow_iso

log = logging.getLogger("bot_indexer")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    problems = validate_production(settings)
    if problems:
        raise RuntimeError("Refusing to start in production: " + " | ".join(problems))

    log_handler = setup_logging("DEBUG" if settings.app_env == "development" else "INFO")
    app.state.log_handler = log_handler
    app.state.started_at = utcnow()
    app.state.settings = settings

    # -- database ----------------------------------------------------------
    db = Database(settings.data_dir)
    db.init()
    app.state.db = db
    repos = Repos(db)
    app.state.repos = repos

    # -- services ------------------------------------------------------------
    auth = AuthService(repos, settings)
    app.state.auth = auth
    app.state.user_service = UserService(repos)

    fetcher = SafeFetcher(
        settings,
        HostRateLimiter(settings.fetch_rate_per_minute, settings.fetch_concurrency_per_host, settings.fetch_host_delay_seconds),
    )
    app.state.fetcher = fetcher

    queue = QueueManager(repos, settings)
    queue.fetcher = fetcher

    # optional integrations (None == NOT CONFIGURED)
    if settings.google_search_console_enabled or settings.google_service_account_json:
        try:
            queue.gsc = GoogleSearchConsole(
                settings.google_service_account_json, settings.public_base_url
            )
        except GSCConfigError as exc:
            log.warning("Google Search Console not initialised: %s", exc)
    app.state.queue = queue

    # -- bootstrap + housekeeping --------------------------------------------
    await bootstrap_admin_if_needed(repos, settings)
    await rebase_page_urls(repos, settings.public_base_url)
    from .database.migrations import repair_status_semantics
    await repair_status_semantics(repos)
    await auth.purge_expired()
    await queue.start()

    log.info(
        "BOT INDEXER %s started (env=%s, data=%s)", __version__, settings.app_env, db.data_dir
    )

    try:
        yield
    finally:
        await queue.stop()
        await fetcher.close()
        log.info("BOT INDEXER stopped")


app = FastAPI(
    title="INDEX MATRIX",
    version=__version__,
    description="PDF URL validation, publishing, discovery & monitoring platform.",
    lifespan=lifespan,
    docs_url=None,
    openapi_url=None,
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Middleware: security headers, CSRF, API rate limiting
# ---------------------------------------------------------------------------

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _parse_cookies(cookie_header: bytes | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_header:
        return out
    for part in cookie_header.decode("latin-1").split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            out[k] = v
    return out


async def _json_send(send, status: int, body: dict) -> None:
    import json

    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class SecurityMiddleware:
    """Security headers + per-IP API rate limit + CSRF enforcement.

    CSRF: double-submit cookie pattern. The first response sets a non-HttpOnly
    ``csrf`` cookie; every state-changing request must echo it in the
    ``X-CSRF-Token`` header (the UI sends it automatically).
    """

    def __init__(self, app):
        self.app = app
        self._api_hits: dict[str, list[float]] = {}
        self._mut_hits: dict[str, list[float]] = {}

    def _limited(self, store: dict[str, list[float]], key: str, limit: int) -> bool:
        now = time.monotonic()
        hits = [t for t in store.get(key, []) if now - t < 60]
        if len(hits) >= limit:
            store[key] = hits
            return True
        hits.append(now)
        store[key] = hits
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        raw_headers = dict(scope.get("headers") or {})
        cookies = _parse_cookies(raw_headers.get(b"cookie"))

        if path.startswith("/api/"):
            # limits are read from live settings (falls back to defaults
            # before the lifespan has started)
            s = getattr(scope.get("app").state, "settings", None)
            if self._limited(self._api_hits, ip, s.api_rate_limit_per_minute if s else 300):
                await _json_send(send, 429, {"detail": "Too many API requests. Slow down."})
                return
            if method not in SAFE_METHODS and self._limited(
                self._mut_hits, ip, s.api_mutation_rate_limit_per_minute if s else 120
            ):
                await _json_send(
                    send, 429, {"detail": "Too many state-changing requests. Slow down."}
                )
                return

        if path in ("/login", "/api/auth/login", "/api/auth/handoff"):
            # temporary-style diagnostic (cookie NAMES only, never values)
            log.info(
                "diag %s %s cookies=%s csrf_header=%s bearer=%s host=%r xfp=%r ua=%r",
                method,
                path,
                sorted(cookies),
                bool(raw_headers.get(b"x-csrf-token")),
                bool((raw_headers.get(b"authorization") or b"").startswith(b"Bearer ")),
                (raw_headers.get(b"host") or b"").decode("latin-1", "ignore")[:60],
                (raw_headers.get(b"x-forwarded-proto") or b"").decode("latin-1", "ignore"),
                (raw_headers.get(b"user-agent") or b"").decode("latin-1", "ignore")[:90],
            )

        if method not in SAFE_METHODS:
            token = (raw_headers.get(b"x-csrf-token") or b"").decode("latin-1")
            cookie_token = cookies.get("csrf")
            if cookie_token is not None:
                # Double-submit check (normal contexts where cookies flow).
                if not token or not hmac.compare_digest(token, cookie_token):
                    await _json_send(send, 403, {"detail": "CSRF token missing or invalid."})
                    return
            # When the request carries NO csrf cookie, this context does not
            # send cookies at all (e.g. a third-party-cookie-blocked iframe):
            # the double-submit pattern cannot apply. Cross-site mutations
            # are still blocked: an attacker page cannot attach the victim's
            # bearer token (custom headers fail the CORS preflight, and the
            # app sets no Access-Control-Allow-* headers), and without a
            # session cookie/bearer the request authenticates as nobody.

        is_page = not path.startswith("/api/") and path not in (
            "/sitemap.xml",
            "/rss.xml",
            "/robots.txt",
        )

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-content-type-options", b"nosniff"))
                if not any(k.lower() == b"referrer-policy" for k, _ in headers):
                    headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                if path.startswith(("/api/auth/", "/api/google-indexing", "/api/real-jobs")):
                    headers.append((b"cache-control", b"no-store"))
                host_name = (raw_headers.get(b"host") or b"").decode("latin-1").split(":")[0]
                if is_page and not host_name.endswith(".e2b.app"):
                    headers.append((b"x-frame-options", b"DENY"))
                forwarded = {k.decode(): v for k, v in (scope.get("headers") or [])}
                secure = (
                    scope.get("scheme") == "https"
                    or forwarded.get("x-forwarded-proto") == "https"
                )
                if secure:
                    headers.append(
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
                    )
                if "csrf" not in cookies:
                    host = (raw_headers.get(b"host") or b"").decode("latin-1")
                    xproto = (raw_headers.get(b"x-forwarded-proto") or b"").decode("latin-1")
                    s = getattr(scope.get("app").state, "settings", None) or get_settings()
                    c_secure, c_samesite = secure_cookie_params(
                        scope.get("scheme"), xproto, host, s
                    )
                    cookie_attrs = f"Path=/; SameSite={c_samesite}"
                    if c_secure:
                        cookie_attrs += "; Secure"
                    headers.append(
                        (
                            b"set-cookie",
                            f"csrf={secrets.token_urlsafe(32)}; {cookie_attrs}".encode(),
                        )
                    )
                message = {**message, "headers": headers}
            if method == "HEAD" and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityMiddleware)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": "Validation error", "errors": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]},
    )


async def http_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse | RedirectResponse:
    if request.url.path.startswith("/api/") or request.headers.get(
        "accept", ""
    ).startswith("application/json"):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})
    if exc.status_code in (302, 303, 307):
        return RedirectResponse(str(exc.headers.get("Location", "/login")), status_code=302)
    if request.url.path.startswith("/"):
        msg = "The page you requested was not found." if exc.status_code == 404 else str(exc.detail)
        return await templates.render_error(request, exc.status_code, msg)
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/api/") or request.headers.get(
        "accept", ""
    ).startswith("application/json"):
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})
    return await templates.render_error(
        request, 500, "Something went wrong. The error has been logged."
    )


app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(StarletteHTTPException, http_handler)
app.add_exception_handler(Exception, server_error_handler)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from .publishing.routes import (  # noqa: E402
    preview_pages,
    router as publishing_router,
)
from .users.routes import page as users_page, router as users_router  # noqa: E402


@app.get("/api/openapi.json", include_in_schema=False)
async def private_openapi(user: dict = Depends(require_admin)):
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
async def private_docs(user: dict = Depends(require_admin)):
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="INDEX MATRIX API")


from .publishing.real_job_routes import router as real_job_router
from .integrations.indexing_routes import router as indexing_router
app.include_router(real_job_router)
app.include_router(indexing_router)
app.include_router(auth_router)
app.include_router(auth_pages)
app.include_router(users_router)
app.include_router(users_page)
app.include_router(publishing_router)
app.include_router(preview_pages)


# ---------------------------------------------------------------------------
# Queue API + page
# ---------------------------------------------------------------------------


@app.get("/api/queue", tags=["queue"])
async def api_queue(request: Request, user: dict = Depends(require_user)):
    queue: QueueManager = request.app.state.queue
    jobs = await queue.list_jobs(limit=200)
    if user.get("role") != "ADMIN":
        repos: Repos = request.app.state.repos
        pdfs = await repos.pdfs.all()
        mine = {p["id"] for p in pdfs if p.get("user_id") == user.get("id")}
        jobs = [j for j in jobs if j.get("pdf_id") in mine or (j.get("pdf_id") is None and j.get("job_type") != "GOOGLE_INDEX_NOTIFY")]
    return {"jobs": jobs}


@app.get("/api/queue/stats", tags=["queue"])
async def api_queue_stats(request: Request, user: dict = Depends(require_user)):
    return await request.app.state.queue.stats()


@app.post("/api/queue/{job_id}/cancel", tags=["queue"])
async def api_queue_cancel(job_id: int, request: Request, user: dict = Depends(require_admin)):
    ok = await request.app.state.queue.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job is not cancellable in its current state.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# System: health, status, logs
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["system"])
async def api_health(request: Request) -> dict:
    db: Database = request.app.state.db
    health = db.health()
    tables_ok = all(t["ok"] for t in health["tables"])
    queue_ok = True
    try:
        await request.app.state.queue.stats()
    except Exception:  # noqa: BLE001
        queue_ok = False
    return {
        "status": "ok" if tables_ok and queue_ok else "degraded",
        "database": "ok" if tables_ok else "error",
        "queue": "ok" if queue_ok else "error",
        "timestamp": utcnow_iso(),
        "version": __version__,
    }


@app.get("/api/system/status", tags=["system"])
async def api_system_status(request: Request, user: dict = Depends(require_admin)):
    settings = request.app.state.settings
    db: Database = request.app.state.db
    tables = {t["table"]: t.get("rows", 0) for t in db.health()["tables"]}
    uptime_s = int((utcnow() - request.app.state.started_at).total_seconds())
    return {
        "app_name": settings.app_name,
        "version": __version__,
        "environment": settings.app_env,
        "public_base_url": settings.public_base_url,
        "uptime_seconds": uptime_s,
        "tables": tables,
        "integrations": {
            "google_search_console": {
                "configured": bool(settings.google_service_account_json),
                "client_loaded": request.app.state.queue.gsc is not None,
            },
            "bing_webmaster": {
                "configured": bool(settings.bing_api_key),
                "client_loaded": request.app.state.queue.bing is not None,
            },
        },
        "concurrency": settings.indexer_concurrency,
        "max_retries": settings.max_retries,
        "no_secrets": True,
    }


@app.get("/api/logs", tags=["system"])
async def api_logs(
    request: Request,
    user: dict = Depends(require_admin),
    level: str | None = None,
    limit: int = 200,
):
    handler: RingLogHandler = request.app.state.log_handler
    lines = handler.lines(limit=min(500, max(10, limit)), level=level)
    events = await request.app.state.repos.events.recent(limit=30)
    return {"lines": lines, "events": events}


@app.get("/logs", include_in_schema=False)
async def logs_page(request: Request, user: dict = Depends(require_admin_page)):
    return templates.render(request, "logs.html", {"user": user})


# ---------------------------------------------------------------------------
# Settings API + page
# ---------------------------------------------------------------------------


@app.get("/api/settings", tags=["settings"])
async def api_settings_get(request: Request, user: dict = Depends(require_admin)):
    out = {}
    for key in RUNTIME_SETTING_KEYS:
        out[key] = await effective_setting(request.app.state.db, key)
    return {"settings": out, "keys": list(RUNTIME_SETTING_KEYS.keys())}


@app.put("/api/settings", tags=["settings"])
async def api_settings_put(payload: dict, request: Request, user: dict = Depends(require_admin)):
    repos: Repos = request.app.state.repos
    saved = {}
    errors = {}
    for key, value in payload.items():
        if key not in RUNTIME_SETTING_KEYS:
            errors[key] = "not a configurable key"
            continue
        parser, _env_key = RUNTIME_SETTING_KEYS[key]
        try:
            parsed = parser(str(value))
        except (ValueError, TypeError):
            errors[key] = "invalid value"
            continue
        clamps = {
            "indexer_concurrency": (1, 5),
            "max_pdf_size_mb": (1, 200),
            "http_timeout": (5, 300),
            "max_redirects": (0, 10),
            "max_retries": (0, 10),
            "polling_interval_ms": (1000, 60000),
            "session_lifetime_hours": (1, 720),
        }
        if key in clamps:
            lo, hi = clamps[key]
            if not (lo <= parsed <= hi):
                errors[key] = f"must be between {lo} and {hi}"
                continue
        await repos.settings.set_key(key, str(value))
        saved[key] = parsed
    if saved:
        await repos.events.add(
            "SETTINGS_UPDATED",
            f"Settings updated by {user.get('email')}: {', '.join(saved)}",
            user_id=user.get("id"),
            status="SUCCESS",
        )
    return {"saved": saved, "errors": errors}


@app.get("/settings", include_in_schema=False)
async def settings_page(request: Request, user: dict = Depends(require_admin_page)):
    current = {}
    for key in RUNTIME_SETTING_KEYS:
        current[key] = await effective_setting(request.app.state.db, key)
    return templates.render(request, "settings.html", {"settings": current, "user": user})


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def home(request: Request):
    user = await get_session_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/references", status_code=302)


class _StaticFiles(StaticFiles):
    """Static files with no-cache for JS/CSS so frontend fixes apply
    without a manual hard refresh."""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).suffix in (".js", ".css"):
            response.headers["cache-control"] = "no-cache"
        return response


app.mount("/static", _StaticFiles(directory=str(STATIC_DIR)), name="static")
