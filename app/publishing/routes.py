"""Public publishing routes: dedicated pages, sitemap, RSS, robots."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .. import templates
from ..auth.routes import require_user_page
from ..config import Settings
from ..database.repositories import Repos
from . import rss as rss_mod
from . import sitemap as sitemap_mod
from .pages import build_pdf_page_context

log = logging.getLogger("bot_indexer.publishing")

router = APIRouter(tags=["publishing"])


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ---------------------------------------------------------------------------
# Public dedicated PDF page
# ---------------------------------------------------------------------------


@router.get("/pdf/{slug}", include_in_schema=False)
async def pdf_dedicated_page(slug: str, request: Request):
    repos = _repos(request)
    pages = await repos.pages.find(lambda p: p.get("slug") == slug)
    if not pages:
        raise HTTPException(status_code=404, detail="Page not found.")
    page = pages[0]
    pdf = await repos.pdfs.get(page["pdf_id"])
    if not pdf:
        raise HTTPException(status_code=404, detail="Underlying PDF record not found.")

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


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------


@router.get("/sitemap.xml", response_class=Response, include_in_schema=False)
async def sitemap(request: Request):
    settings = _settings(request)
    if not await _effective_sitemap_enabled(request):
        raise HTTPException(status_code=404, detail="Sitemap is disabled.")
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("sitemap_included")]
    base = settings.public_base_url
    if len(pages) > sitemap_mod.SITEMAP_CHUNK_SIZE:
        # Sitemap index + first chunk
        return Response(sitemap_mod.render_sitemap_index(base, len(pages), pages[-1].get("updated_at", "")), media_type="application/xml")
    latest = pages[-1].get("updated_at") if pages else None
    return Response(sitemap_mod.render_sitemap(pages, base, chunk=1), media_type="application/xml")


@router.get("/sitemap-{chunk}.xml", response_class=Response, include_in_schema=False)
async def sitemap_chunk(chunk: int, request: Request):
    repos = _repos(request)
    settings = _settings(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("sitemap_included")]
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


@router.get("/rss.xml", response_class=Response, include_in_schema=False)
async def rss_feed(request: Request):
    settings = _settings(request)
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="published_at") if p.get("rss_included")]
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
        "Allow: /sitemap.xml\n"
        "Allow: /rss.xml\n"
        "Disallow: /api/\n"
        "Disallow: /login\n"
        f"\nSitemap: {base}/sitemap.xml\n"
    )


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots(request: Request):
    return PlainTextResponse(render_robots(_settings(request)))


# ---------------------------------------------------------------------------
# Admin preview pages (HTML wrappers around the XML endpoints)
# ---------------------------------------------------------------------------

preview_pages = APIRouter(tags=["publishing-pages"])


@preview_pages.get("/sitemap", include_in_schema=False)
async def sitemap_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="-published_at") if p.get("sitemap_included")]
    return templates.render(
        request,
        "sitemap_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )


@preview_pages.get("/rss", include_in_schema=False)
async def rss_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    pages = [p for p in await repos.pages.all(order="-published_at") if p.get("rss_included")]
    return templates.render(
        request,
        "rss_page.html",
        {"pages": pages, "base_url": _settings(request).public_base_url},
    )
