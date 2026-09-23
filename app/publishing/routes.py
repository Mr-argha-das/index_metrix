"""Public publishing routes for vacancy pages and discovery feeds."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .. import templates
from ..auth.routes import require_user_page
from ..config import Settings
from ..database.repositories import Repos, effective_setting
from . import rss as rss_mod
from . import sitemap as sitemap_mod
from .pages import public_page_path
from .real_jobs import discoverable
from .real_job_routes import render_real_job

router = APIRouter(tags=["publishing"])


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.api_route("/jobs/{number}", include_in_schema=False, methods=["GET", "HEAD"])
async def job_dedicated_page(number: str, request: Request):
    if not number.isascii() or not number.isdigit() or len(number) > 18 or str(int(number)) != number:
        raise HTTPException(404, "Job page not found.")
    pages = await _repos(request).pages.find(
        lambda p: p.get("page_kind") == "real-job" and p.get("job_number") == int(number)
    )
    if not pages:
        raise HTTPException(404, "Job page not found.")
    return render_real_job(request, pages[0])


@router.api_route("/sitemap.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def sitemap(request: Request):
    settings = _settings(request)
    if not await _effective_sitemap_enabled(request):
        raise HTTPException(status_code=404, detail="Sitemap is disabled.")
    pages = [
        p for p in await _repos(request).pages.all(order="published_at")
        if p.get("sitemap_included") and discoverable(p)
    ]
    base = settings.public_base_url
    if len(pages) > sitemap_mod.SITEMAP_CHUNK_SIZE:
        return Response(
            sitemap_mod.render_sitemap_index(
                base, len(pages), max((p.get("updated_at") or "" for p in pages), default="")
            ),
            media_type="application/xml",
        )
    return Response(sitemap_mod.render_sitemap(pages, base, chunk=1), media_type="application/xml")


@router.api_route("/sitemap-{chunk}.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def sitemap_chunk(chunk: int, request: Request):
    settings = _settings(request)
    pages = [
        p for p in await _repos(request).pages.all(order="published_at")
        if p.get("sitemap_included") and discoverable(p)
    ]
    max_chunk = max(1, (len(pages) + sitemap_mod.SITEMAP_CHUNK_SIZE - 1) // sitemap_mod.SITEMAP_CHUNK_SIZE)
    if not await _effective_sitemap_enabled(request) or chunk < 1 or chunk > max_chunk:
        raise HTTPException(404, "Sitemap chunk not found.")
    start = (chunk - 1) * sitemap_mod.SITEMAP_CHUNK_SIZE
    return Response(
        sitemap_mod.render_sitemap(pages[start:start + sitemap_mod.SITEMAP_CHUNK_SIZE], settings.public_base_url, chunk=chunk),
        media_type="application/xml",
    )


async def _effective_sitemap_enabled(request: Request) -> bool:
    try:
        return bool(await effective_setting(request.app.state.db, "sitemap_enabled"))
    except Exception:
        return _settings(request).sitemap_enabled


@router.api_route("/rss.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def rss_feed(request: Request):
    if not await effective_setting(request.app.state.db, "rss_enabled"):
        raise HTTPException(status_code=404, detail="RSS is disabled.")
    settings = _settings(request)
    pages = [
        p for p in await _repos(request).pages.all(order="published_at")
        if p.get("rss_included") and discoverable(p)
    ]
    return Response(rss_mod.render_rss(pages, settings), media_type="application/rss+xml")


def render_robots(settings: Settings) -> str:
    base = settings.public_base_url
    return (
        "User-agent: *\n"
        "Allow: /jobs/\n"
        "Allow: /sitemap.xml\n"
        "Allow: /rss.xml\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /data/\n"
        "Disallow: /login\n"
        f"\nSitemap: {base}/sitemap.xml\n"
    )


@router.api_route("/robots.txt", response_class=PlainTextResponse, include_in_schema=False, methods=["GET", "HEAD"])
async def robots(request: Request):
    return PlainTextResponse(render_robots(_settings(request)))


preview_pages = APIRouter(tags=["publishing-pages"])


@preview_pages.get("/sitemap", include_in_schema=False)
async def sitemap_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [
        p for p in await repos.pages.all(order="-published_at")
        if p.get("sitemap_included") and discoverable(p)
    ]
    return templates.render(
        request,
        "sitemap_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )


@preview_pages.get("/rss", include_in_schema=False)
async def rss_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [
        p for p in await repos.pages.all(order="-published_at")
        if p.get("rss_included") and discoverable(p)
    ]
    return templates.render(
        request,
        "rss_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )
