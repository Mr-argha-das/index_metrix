"""RSS 2.0 feed of newly published dedicated pages.

``pubDate`` is the real publication timestamp of each page — it is never
bumped without an actual change.
"""
from __future__ import annotations

from email.utils import formatdate
from xml.sax.saxutils import escape

from ..utils import parse_iso, truncate


def _pubdate(iso: str | None) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return formatdate(usegmt=True)
    return formatdate(timeval=dt.timestamp(), usegmt=True)


def render_rss(pages: list[dict], settings, limit: int | None = None) -> str:
    limit = limit or settings.rss_max_items
    pages = sorted(
        pages, key=lambda p: p.get("published_at") or "", reverse=True
    )[:limit]

    base = settings.public_base_url
    now = _pubdate(None)
    items = []
    for p in pages:
        # Keep every feed entry canonical when the public domain changes.
        link = f"{base.rstrip('/')}/pdf/{p.get('slug')}"
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
        f"    <title>{escape(settings.app_name)} — Published PDF pages</title>\n"
        f"    <link>{escape(base)}</link>\n"
        f"    <atom:link href=\"{escape(base + '/rss.xml')}\" rel=\"self\" type=\"application/rss+xml\"/>\n"
        f"    <description>RSS feed of dedicated pages for validated PDF documents, published on {escape(settings.app_name)}.</description>\n"
        f"    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
