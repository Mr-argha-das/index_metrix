"""Jinja2 template rendering helper with security defaults."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.templating import Jinja2Templates as _J2

log = logging.getLogger("bot_indexer.templates")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

_templates: _J2 | None = None


_PILL_COLORS = {
    "VALID": "green", "PDF_VALID": "green", "PAGE_PUBLISHED": "green", "TEXT_PDF": "green",
    "RECEIVED": "blue", "VALIDATING": "amber", "PDF_ANALYZING": "amber", "PAGE_GENERATING": "amber",
    "INVALID": "red", "PDF_INVALID": "red", "PDF_ANALYSIS_FAILED": "red", "FAILED": "red",
    "PAGE_FAILED": "red", "DISCOVERY_PENDING": "cyan", "DISCOVERY_SUBMITTED": "violet",
    "FETCH_CHECKED": "blue", "SEARCH_ENGINE_CRAWL_EVIDENCE": "violet",
    "DONE": "green", "DISCOVERED": "green",
    "CRAWL_UNKNOWN": "slate", "CRAWL_OBSERVED": "blue", "CRAWL_CHECKED": "violet",
    "INDEX_UNKNOWN": "slate", "INDEXED": "green", "NOT_INDEXED": "red",
    "SCANNED_OR_EMPTY_PDF": "amber", "ERROR": "red",
    "CONNECTED": "green", "NOT CONNECTED": "slate", "NOT CONFIGURED": "slate",
    "NOT_AUTHORIZED": "amber", "NOT AUTHORIZED": "amber",
}


def _pill_html(status: str, color: str | None = None) -> str:
    from markupsafe import Markup

    status = (status or "").upper()
    c = color or _PILL_COLORS.get(status, "slate")
    label = status.replace("_", " ") or "N/A"
    return Markup(f'<span class="pill {c}"><span class="dot"></span>{label}</span>')


def _integration_pill(status: str) -> str:
    return _pill_html(status)


def get_templates() -> _J2:
    global _templates
    if _templates is None:
        env = _J2(TEMPLATES_DIR)
        jenv = env.env  # the underlying Jinja2 Environment
        from .publishing.pages import public_page_path
        jenv.globals["reference_path"] = public_page_path
        jenv.globals["static_url"] = "/static"
        jenv.globals["integration_pill"] = _integration_pill
        jenv.filters["integration_pill"] = _integration_pill
        jenv.globals["pill_status"] = _pill_html
        jenv.globals["pill2"] = lambda color, label: _pill_html(label, color=color)
        _templates = env
    return _templates


def _security_headers(request: Request) -> dict:
    settings = request.app.state.settings
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",

    }
    if not request.url.hostname or not request.url.hostname.endswith(".e2b.app"):
        headers["X-Frame-Options"] = "DENY"
    if settings.secure_cookies:
        headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return headers


def render(request: Request, name: str, context: dict | None = None) -> HTMLResponse:
    """Render a template with autoescape on (Jinja2 default) + base context."""
    templates = get_templates()
    ctx = {
        "request": request,
        "app_name": request.app.state.settings.app_name,
        "settings": request.app.state.settings,
        "current_user": getattr(request.state, "session_user", None),
        "csrf_token": request.cookies.get("csrf", ""),
        "title": None,
        "subtitle": None,
        "nav": None,
        "bootstrap_session": getattr(request.state, "bootstrap_session", None),
        **(context or {}),
    }
    if "user" in ctx and ctx["user"] is not None:
        ctx["current_user"] = ctx["user"]
    resp = templates.TemplateResponse(request, name, ctx)
    for k, v in _security_headers(request).items():
        resp.headers[k] = v
    if ctx["current_user"]:
        resp.headers["Cache-Control"] = "no-store, private"
        resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


async def render_error(request: Request, status_code: int, message: str) -> HTMLResponse:
    templates = get_templates()
    name = (
        f"errors/{status_code}.html"
        if (TEMPLATES_DIR / f"errors/{status_code}.html").exists()
        else "errors/404.html"
    )
    return templates.TemplateResponse(
        request,
        name,
        {
            "request": request,
            "app_name": request.app.state.settings.app_name,
            "status_code": status_code,
            "message": message,
        },
        status_code=status_code,
    )


def template_exists(name: str) -> bool:
    return (TEMPLATES_DIR / name).exists()
