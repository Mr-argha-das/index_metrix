"""RSS 2.0 feed of newly published dedicated pages.

``pubDate`` is the real publication timestamp of each page — it is never
bumped without an actual change.
"""
from __future__ import annotations

from .pages import public_page_url

from email.utils import formatdate
from xml.sax.saxutils import escape as _escape


def escape(value: str) -> str:
    # PDF metadata may contain control characters forbidden by XML 1.0.
    return _escape("".join(c for c in value if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF), {'"': "&quot;"})

from ..utils import parse_iso, truncate


def _pubdate(iso: str | None) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return ""
    return formatdate(timeval=dt.timestamp(), usegmt=True)


def render_rss(pages: list[dict], settings, limit: int | None = None) -> str:
    limit = limit or settings.rss_max_items
    pages = sorted(
        pages, key=lambda p: p.get("published_at") or "", reverse=True
    )[:limit]

    base = settings.public_base_url
    last_build = max((p.get("updated_at") or p.get("published_at") or "" for p in pages), default="")
    build_tag = f"    <lastBuildDate>{_pubdate(last_build)}</lastBuildDate>\n" if _pubdate(last_build) else ""
    items = []
    for p in pages:
        if not _pubdate(p.get("published_at")):
            continue  # no fabricated publication dates
        # Keep every feed entry canonical when the public domain changes.
        link = public_page_url(base, p)
        guid = link
        items.append(
            "  <item>\n"
            f"    <title>{escape(p.get('title') or 'PDF page')}</title>\n"
            f"    <link>{escape(link)}</link>\n"
            f"    <guid isPermaLink=\"true\">{escape(guid)}</guid>\n"
            f"    <description>{escape(truncate(p.get('description') or '', 400))}</description>\n"
            f"    <pubDate>{_pubdate(p.get('published_at'))}</pubDate>\n"
            "  </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(settings.app_name)} — Published reference pages</title>\n"
        f"    <link>{escape(base)}</link>\n"
        f"    <atom:link href=\"{escape(base + '/rss.xml')}\" rel=\"self\" type=\"application/rss+xml\"/>\n"
        f"    <description>Real vacancies, generated job examples and source reference pages, published on {escape(settings.app_name)}.</description>\n"
        f"    <language>en</language>\n"
        f"{build_tag}"
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
