"""Sitemap generation.

Only OUR OWN dedicated pages are listed — never third-party PDF URLs.
``<lastmod>`` is the page's real ``updated_at`` timestamp. When the page
count exceeds one chunk (50 000 URLs), ``/sitemap.xml`` becomes a sitemap
index pointing at ``/sitemap-N.xml`` chunks.
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

SITEMAP_CHUNK_SIZE = 50_000


def _url_entry(url: str, lastmod: str | None) -> str:
    lastmod_xml = f"\n  <lastmod>{lastmod}</lastmod>" if lastmod else ""
    return f"  <url>\n  <loc>{escape(url)}</loc>{lastmod_xml}\n  </url>"


def render_sitemap(pages: list[dict], base_url: str, chunk: int = 1) -> str:
    """Render one sitemap (chunk) from a list of page rows."""
    urls = []
    for p in pages:
        # A page URL is derived from the currently configured public origin.
        # Do not reuse a historical stored hostname after a domain migration.
        page_url = f"{base_url.rstrip('/')}/pdf/{p.get('slug')}"
        lastmod = p.get("updated_at") or p.get("published_at")
        urls.append(_url_entry(page_url, lastmod))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


def render_sitemap_index(base_url: str, count: int, updated_at: str) -> str:
    chunks = (count - 1) // SITEMAP_CHUNK_SIZE + 1
    entries = []
    for i in range(chunks):
        entries.append(
            f"  <sitemap>\n  <loc>{escape(f'{base_url}/sitemap-{i + 1}.xml')}</loc>\n"
            f"  <lastmod>{escape(updated_at)}</lastmod>\n  </sitemap>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</sitemapindex>\n"
    )
