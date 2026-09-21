"""Dedicated-page generation for validated PDFs.

Each valid PDF gets one useful, SEO-complete page on OUR domain
(``/pdf/<slug>``). The page links to the original third-party PDF with a
normal ``<a>`` tag and never pretends to host the file.
"""
from __future__ import annotations

import logging

from ..database.repositories import Repos
from ..pdf.analyzer import AnalysisResult
from ..utils import json_dumps, jsonable, short_hash, slugify, truncate, utcnow_iso

log = logging.getLogger("bot_indexer.pages")

MAX_SLUG_LEN = 72


def public_page_url(base_url: str, slug: str) -> str:
    """Return the current canonical URL for a dedicated page.

    Page URLs are derived data. Recomputing them from the active public origin
    keeps pages published before a domain move out of the sitemap, RSS and
    canonical metadata under their former hostname.
    """
    return f"{base_url.rstrip('/')}/pdf/{slug}"


async def rebase_page_urls(repos: Repos, base_url: str) -> int:
    """Persist the current canonical origin on previously published pages."""
    updated = 0
    for page in await repos.pages.all():
        desired_url = public_page_url(base_url, page["slug"])
        if page.get("page_url") != desired_url:
            await repos.pages.update(page["id"], page_url=desired_url)
            updated += 1
    if updated:
        log.info("Rebased %s published page URL(s) to %s", updated, base_url)
    return updated


def base_slug_for(pdf_title: str | None, url: str) -> str:
    """Stable slug base derived only from the normalized URL filename, never
    mutable PDF metadata. The unique suffix (hash of the normalized URL) is appended by
    the caller, so slugs are stable across restarts and unique by
    construction."""
    try:
        from urllib.parse import unquote, urlsplit

        path = urlsplit(url).path
        name = unquote(path.rsplit("/", 1)[-1])
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        base = slugify(name, max_len=MAX_SLUG_LEN - 10)
    except Exception:  # noqa: BLE001
        base = ""
    return base or "pdf-document"


def make_slug(base: str, normalized_url: str) -> str:
    from ..pdf.validator import normalize_url

    suffix = short_hash(normalize_url(normalized_url), 12)
    return f"{base[:MAX_SLUG_LEN - 13]}-{suffix}"


def page_description(analysis: AnalysisResult | None, pdf: dict, url: str) -> str:
    """Generate an honest, useful description for the dedicated page and RSS."""
    domain = pdf.get("source_domain") or "unknown source"
    parts = []
    title = (analysis.title if analysis else None) or (pdf.get("title") or "PDF document")
    parts.append(f"Validated PDF reference for “{title}” from {domain}.")
    if analysis and analysis.page_count:
        parts.append(f"{analysis.page_count} page(s).")
    if analysis and analysis.classification == "TEXT_PDF":
        parts.append("Text-based PDF; metadata extracted automatically.")
    elif analysis and analysis.classification == "SCANNED_OR_EMPTY_PDF":
        parts.append("Scanned or image-based PDF; text extraction limited.")
    if analysis and analysis.first_page_text:
        parts.append("Preview: " + truncate(analysis.first_page_text, 180))
    parts.append("Original file is linked on the page; it is hosted by the source publisher.")
    return truncate(" ".join(parts), 300)


async def create_page_for_pdf(
    repos: Repos,
    settings,
    pdf: dict,
    analysis: AnalysisResult | None,
) -> dict:
    """Create (or return the existing) dedicated page for a PDF record."""
    existing = await repos.pages.find(lambda p: p.get("pdf_id") == pdf["id"])
    if existing:
        page = existing[0]
        title = truncate((analysis.title if analysis else None) or pdf.get("title") or page["title"], 200)
        description = page_description(analysis, pdf, pdf["normalized_url"])
        if title != page["title"] or description != page["description"] or page.get("pdf_sha256") != pdf.get("sha256"):
            page = await repos.pages.update(page["id"], title=title, description=description, pdf_sha256=pdf.get("sha256"))
        return page

    base = base_slug_for((analysis.title if analysis else None) or pdf.get("title"), pdf["normalized_url"])
    slug = make_slug(base, pdf["normalized_url"])
    # Defensive uniqueness (hash suffix already makes collisions unlikely)
    taken = {p.get("slug") for p in await repos.pages.all()}
    n = 2
    while slug in taken:
        slug = make_slug(base, pdf["normalized_url"]) + str(n)
        n += 1

    base_url = settings.public_base_url
    title = (analysis.title if analysis else None) or pdf.get("title") or f"{base.replace('-', ' ')} — PDF from {pdf.get('source_domain')}"
    description = page_description(analysis, pdf, pdf["normalized_url"])
    now = utcnow_iso()

    page = await repos.pages.insert(
        pdf_id=pdf["id"],
        pdf_sha256=pdf.get("sha256"),
        slug=slug,
        page_url=public_page_url(base_url, slug),
        title=truncate(title, 200),
        description=description,
        published_at=now,
        updated_at=now,
        sitemap_included=bool(settings.sitemap_enabled),
        rss_included=bool(settings.rss_enabled),
    )
    await repos.events.add(
        "PAGE_CREATED",
        f"Dedicated page published: /pdf/{slug}",
        pdf_id=pdf["id"],
        status="SUCCESS",
        metadata={"slug": slug, "page_url": page["page_url"]},
    )
    log.info("pdf_id=%s event=PAGE_CREATED slug=%s", pdf["id"], slug)
    return page


def build_pdf_page_context(pdf: dict, page: dict, analysis: AnalysisResult | None, settings) -> dict:
    """Context for the public dedicated-page template. All remote-derived
    strings are rendered through Jinja2 autoescaping."""
    from ..utils import json_loads, parse_iso

    def fmt_date(value):
        if not value:
            return None
        dt = parse_iso(value)
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        # PDF internal dates like D:20240101120000+01'00'
        try:
            raw = str(value).replace("D:", "")[:14]
            from datetime import datetime

            return datetime.strptime(raw, "%Y%m%d%H%M%S").strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return str(value)

    canonical_url = public_page_url(settings.public_base_url, page["slug"])
    return {
        "pdf": jsonable(pdf),
        "page": jsonable(page),
        "app_name": settings.app_name,
        "base_url": settings.public_base_url,
        "title": page.get("title") or "PDF document",
        "description": page.get("description") or "",
        "page_url": canonical_url,
        "canonical_url": canonical_url,
        "original_url": pdf.get("normalized_url") or pdf.get("original_url"),
        "source_domain": pdf.get("source_domain"),
        "page_count": pdf.get("page_count"),
        "file_size": pdf.get("content_length"),
        "author": (analysis.author if analysis else None) or pdf.get("author"),
        "created_date": fmt_date((analysis.creation_date if analysis else None) or pdf.get("created_date")),
        "modified_date": fmt_date((analysis.modification_date if analysis else None) or pdf.get("modified_date")),
        "producer": (analysis.producer if analysis else None) or pdf.get("producer"),
        "classification": pdf.get("classification"),
        "text_length": pdf.get("text_length"),
        "published_at": fmt_date(page.get("published_at")),
        "updated_at": fmt_date(page.get("updated_at")),
        "excerpt": truncate((analysis.first_page_text if analysis else None) or pdf.get("first_page_text"), 600),
        "sha256": pdf.get("sha256"),
        "metadata_json": json_dumps(
            {
                "title": pdf.get("title"),
                "author": pdf.get("author"),
                "subject": pdf.get("subject"),
                "creator": pdf.get("creator"),
                "producer": pdf.get("producer"),
            }
        ),
        # inline JSON must not be HTML-escaped (browsers don't decode
        # entities inside <script>) and must not contain a closing </script>
        "jsonld": json_dumps(
            {
                "@context": "https://schema.org",
                "@type": "WebPage",
                "name": page.get("title"),
                "description": page.get("description"),
                "url": canonical_url,
                "datePublished": page.get("published_at"),
                "dateModified": page.get("updated_at"),
                "about": {
                    "@type": "DigitalDocument",
                    "name": pdf.get("title") or page.get("title"),
                    "url": pdf.get("original_url"),
                    "contentUrl": pdf.get("original_url"),
                    **({"author": {"@type": "Person", "name": pdf.get("author")}} if pdf.get("author") else {}),
                    **({"numberOfPages": int(pdf["page_count"])} if pdf.get("page_count") else {}),
                },
            }
        ).replace("</", "<\\/"),
    }
