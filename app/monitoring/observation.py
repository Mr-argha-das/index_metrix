"""Search visibility observation — explicitly NON-authoritative.

Performs plain web-search queries (exact URL / ``site:domain``) and records:

* ``OBSERVED``       — the exact URL appeared in results
* ``NOT_OBSERVED``   — query succeeded, URL absent
* ``UNKNOWN``        — the search engine blocked the query (captcha, 429, …)

Results are always labelled as *observation*, never as Search Console
evidence, and can never set index status to INDEXED.
"""
from __future__ import annotations

import logging
from urllib.parse import quote_plus

from ..pdf.fetcher import FetchError

from ..queue.manager import QueueManager
from ..utils import utcnow_iso

log = logging.getLogger("bot_indexer.observation")

UA = "BOT-INDEXER/1.0 (search visibility observation; non-authoritative)"


async def _query(fetcher, query: str) -> tuple[str, str]:
    """Returns (result_state, reason)."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&num=20"
    try:
        if fetcher is None:
            return "UNKNOWN", "Safe fetcher is not configured."
        resp = await fetcher.fetch(url, max_bytes=2 * 1024 * 1024)
    except FetchError as exc:
        return "UNKNOWN", f"Search query failed: {exc.__class__.__name__}"
    if resp.status != 200:
        return "UNKNOWN", f"Search engine returned HTTP {resp.status} (query blocked)."
    text = resp.content.decode("utf-8", errors="replace")
    if "unusual traffic" in text or "captcha" in text.lower() or "sorry." in text.lower():
        return "UNKNOWN", "Search engine blocked the automated query (captcha/consent)."
    return "200", text


def _contains_url(html: str, url: str) -> bool:
    from bs4 import BeautifulSoup
    from urllib.parse import urlsplit, parse_qs
    # A reflected query string is not a result. Only examine actual links.
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = link["href"]
        if href.startswith("/url?"):
            query = parse_qs(urlsplit(href).query)
            href = (query.get("q") or query.get("url") or [""])[0]
        if href.rstrip("/") == url.rstrip("/"):
            return True
    return False


async def run_observation(manager: QueueManager, pdf: dict) -> dict:
    """Run observation checks for a PDF. Returns a result dict for the event."""
    settings = manager.settings
    target = pdf.get("normalized_url") or pdf.get("original_url") or ""
    domain = pdf.get("source_domain") or ""

    # Even optional observations use robots-aware, bounded, non-spoofed HTTP.
    state, detail = await _query(manager.fetcher, f'"{target}"')
    if state != "200":
        return {
            "message": f"Search visibility observation: UNKNOWN — {detail}",
            "metadata": {
                "label": "Search visibility observation (NOT authoritative)",
                "state": "UNKNOWN",
                "detail": detail,
                "checked_at": utcnow_iso(),
            },
        }
    exact_hit = _contains_url(detail, target)
    site_state = site_hit = None
    if domain:
        state2, detail2 = await _query(manager.fetcher, f"site:{domain} {target.rsplit('/', 1)[-1][:60]}")
        if state2 == "200":
            site_state = "OK"
            site_hit = target in detail2
        else:
            site_state = detail2

    if exact_hit:
        overall = "OBSERVED"
        message = (
            "Search visibility observation: the exact URL appeared in public "
            "search results. This is an OBSERVATION only — it is NOT Search "
            "Console evidence and does not prove indexing of the PDF."
        )
    else:
        overall = "NOT_OBSERVED"
        message = (
            "Search visibility observation: the exact URL did not appear in "
            "public search results. Absence from one query is not proof of "
            "non-indexing (results vary by region/time)."
        )
    return {
        "message": message,
        "metadata": {
            "label": "Search visibility observation (NOT authoritative)",
            "state": overall,
            "exact_url_query": exact_hit,
            "site_query": site_state,
            "checked_at": utcnow_iso(),
        },
    }
