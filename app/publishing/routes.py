"""Public publishing routes: dedicated pages, sitemap, RSS, robots."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, RedirectResponse

from .. import templates
from ..auth.routes import require_user_page
from ..config import Settings
from ..database.repositories import Repos
from . import rss as rss_mod
from . import sitemap as sitemap_mod
from .pages import build_pdf_page_context, public_page_path

log = logging.getLogger("bot_indexer.publishing")

from .real_jobs import discoverable

router = APIRouter(tags=["publishing"])


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ---------------------------------------------------------------------------
# Public dedicated PDF page
# ---------------------------------------------------------------------------


@router.api_route("/pdf/{slug}", include_in_schema=False, methods=["GET", "HEAD"])
async def pdf_dedicated_page(slug: str, request: Request):
    repos = _repos(request)
    pages = await repos.pages.find(lambda p: p.get("slug") == slug)
    if not pages:
        raise HTTPException(status_code=404, detail="Page not found.")
    page = pages[0]
    if page.get("page_kind") == "real-job":
        return RedirectResponse(public_page_path(page), status_code=308)
    pdf = await repos.pdfs.get(page["pdf_id"])
    if not pdf:
        raise HTTPException(status_code=404, detail="Underlying PDF record not found.")

    if page.get("page_kind") == "demo-job":
        return RedirectResponse(public_page_path(page), status_code=308)

    from ..pdf.analyzer import AnalysisResult

    analysis = None
    # Rebuild a lightweight analysis context from stored metadata (no re-parse).
    if pdf.get("classification") in ("TEXT_PDF", "SCANNED_OR_EMPTY_PDF", None):
        analysis = AnalysisResult(
            ok=True,
            title=pdf.get("title"),
            author=pdf.get("author"),
            subject=pdf.get("subject"),
            creator=pdf.get("creator"),
            producer=pdf.get("producer"),
            creation_date=pdf.get("created_date"),
            modification_date=pdf.get("modified_date"),
            text_length=pdf.get("text_length"),
            first_page_text=pdf.get("first_page_text"),
            language=pdf.get("language"),
            classification=pdf.get("classification"),
        )
    ctx = build_pdf_page_context(pdf, page, analysis, _settings(request))
    return templates.render(request, "pdf_page.html", ctx)


@router.api_route("/jobs/{number}", include_in_schema=False, methods=["GET", "HEAD"])
async def job_dedicated_page(number: str, request: Request):
    if not number.isascii() or not number.isdigit() or len(number) > 18 or str(int(number)) != number:
        raise HTTPException(404, "Job page not found.")
    pages = await _repos(request).pages.find(
        lambda p: p.get("page_kind") in ("demo-job", "real-job") and p.get("job_number") == int(number))
    if not pages:
        raise HTTPException(404, "Job page not found.")
    page = pages[0]
    if page.get("page_kind") == "real-job":
        from .real_job_routes import render_real_job
        return render_real_job(request, page)
    pdf = await _repos(request).pdfs.get(page["pdf_id"])
    if not pdf:
        raise HTTPException(404, "Source record not found.")
    ctx = build_pdf_page_context(pdf, page, None, _settings(request))
    if not ctx["demo_job"] or ctx["demo_job"].get("isFictional") is not True:
        raise HTTPException(503, "Stored generated job content unavailable.")
    return templates.render(request, "pdf_page.html", ctx)


@router.api_route("/references", include_in_schema=False, methods=["GET", "HEAD"])
async def reference_library(request: Request, page: int = Query(1, ge=1)):
    """Public, server-rendered discovery links, not an authenticated dashboard."""
    rows = [p for p in await _repos(request).pages.all(order="id") if discoverable(p)]
    per_page = 50
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    if page > total_pages:
        raise HTTPException(404, "Reference library page not found.")
    base = _settings(request).public_base_url
    return templates.render(request, "reference_library.html", {
        "pages": rows[(page - 1) * per_page:page * per_page],
        "number": page, "total_pages": total_pages,
        "canonical_url": base + "/references" + (f"?page={page}" if page > 1 else ""),
    })


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------


@router.api_route("/sitemap.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def sitemap(request: Request):
    settings = _settings(request)
    if not await _effective_sitemap_enabled(request):
        raise HTTPException(status_code=404, detail="Sitemap is disabled.")
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("sitemap_included") and discoverable(p)]
    base = settings.public_base_url
    if len(pages) > sitemap_mod.SITEMAP_CHUNK_SIZE:
        # Sitemap index + first chunk
        return Response(sitemap_mod.render_sitemap_index(base, len(pages), max((p.get("updated_at") or "" for p in pages), default="")), media_type="application/xml")
    return Response(sitemap_mod.render_sitemap(pages, base, chunk=1), media_type="application/xml")


@router.api_route("/sitemap-{chunk}.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def sitemap_chunk(chunk: int, request: Request):
    repos = _repos(request)
    settings = _settings(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("sitemap_included") and discoverable(p)]
    if not await _effective_sitemap_enabled(request) or chunk < 1 or chunk > max(1, (len(pages) + sitemap_mod.SITEMAP_CHUNK_SIZE - 1) // sitemap_mod.SITEMAP_CHUNK_SIZE):
        raise HTTPException(status_code=404, detail="Sitemap chunk not found.")
    start = (chunk - 1) * sitemap_mod.SITEMAP_CHUNK_SIZE
    part = pages[start : start + sitemap_mod.SITEMAP_CHUNK_SIZE]
    return Response(
        sitemap_mod.render_sitemap(part, settings.public_base_url, chunk=chunk),
        media_type="application/xml",
    )


async def _effective_sitemap_enabled(request: Request) -> bool:
    from ..database.repositories import effective_setting

    try:
        return bool(await effective_setting(request.app.state.db, "sitemap_enabled"))
    except Exception:  # noqa: BLE001
        return _settings(request).sitemap_enabled


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


@router.api_route("/rss.xml", response_class=Response, include_in_schema=False, methods=["GET", "HEAD"])
async def rss_feed(request: Request):
    settings = _settings(request)
    from ..database.repositories import effective_setting
    if not await effective_setting(request.app.state.db, "rss_enabled"):
        raise HTTPException(status_code=404, detail="RSS is disabled.")
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("rss_included") and discoverable(p)]
    return Response(rss_mod.render_rss(pages, settings), media_type="application/rss+xml")


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def render_robots(settings: Settings) -> str:
    base = settings.public_base_url
    return (
        "# BOT INDEXER robots.txt\n"
        "# Dedicated PDF pages and feeds are meant to be discovered and crawled.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /pdf/\n"
        "Allow: /jobs/\n"
        "Allow: /references\n"
        "Allow: /sitemap.xml\n"
        "Allow: /rss.xml\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /data/\n"
        "Disallow: /login\n"
        "Disallow: /real-jobs\n"
        "Disallow: /google-indexing\n"
        f"\nSitemap: {base}/sitemap.xml\n"
    )


@router.api_route("/robots.txt", response_class=PlainTextResponse, include_in_schema=False, methods=["GET", "HEAD"])
async def robots(request: Request):
    return PlainTextResponse(render_robots(_settings(request)))


# ---------------------------------------------------------------------------
# Admin preview pages (HTML wrappers around the XML endpoints)
# ---------------------------------------------------------------------------

preview_pages = APIRouter(tags=["publishing-pages"])


@preview_pages.get("/sitemap", include_in_schema=False)
async def sitemap_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="-published_at") if p.get("sitemap_included") and discoverable(p)]
    return templates.render(
        request,
        "sitemap_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )


@preview_pages.get("/rss", include_in_schema=False)
async def rss_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="-published_at") if p.get("rss_included") and discoverable(p)]
    return templates.render(
        request,
        "rss_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )
