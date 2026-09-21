"""Evidence-based status logic.

The single most important rule in the application:

    **Index/crawl status only changes when legitimate, authorized evidence
    exists — and the UI must always show where that evidence came from.**

* Our server fetching a URL  → technical probe (NEVER "Google crawl")
* Our page in sitemap/RSS    → discovery pending (NEVER "indexed")
* Search result observation  → OBSERVED / NOT_OBSERVED (NEVER "indexed")
* Authorized Search Console data for OUR property → the only source that can
  set INDEXED / NOT_INDEXED for our pages, with the check timestamp and the
  exact API values stored as evidence.

Third-party PDFs can never be indexed by us; for their *own* domains we have
no authorized property, so their status is NOT_AUTHORIZED / UNKNOWN and the
reason is recorded.
"""
from __future__ import annotations

import logging

from ..database.repositories import Repos
from ..queue.worker import (
    CS_CHECKED,
    CS_OBSERVED,
    CS_UNKNOWN,
    IS_INDEXED,
    IS_NOT_INDEXED,
    IS_UNKNOWN,
)
from ..utils import json_dumps, utcnow_iso

log = logging.getLogger("bot_indexer.monitoring")

# Mapping from Google Search Console URL Inspection coverage states to our
# index status. Only these states are authoritative.
_GSC_INDEXED_STATES = {
    "Submitted / Indexed",
    "Published",
    "IndexingRequested",
}
_GSC_NOT_INDEXED_STATES = {
    "Submitted / Crawled - currently not indexed",
    "Crawled - currently not indexed",
    "Crawled - currently not indexed due to meta robots tag",
    "Excluded / Blocked by robots.txt",
    "Excluded / Blocked by X-Robots-Tag",
    "Excluded / Page with redirect",
    "Excluded / Duplicate without canonical",
    "Excluded / Duplicate, Google has preferred another URL",
    "Excluded / Not in sitemap",
    "Excluded / Deprecated for search",
    "Excluded / Other",
    "Crawled - currently not indexed (soft 404)",
}


def classify_gsc_index_state(inspection: dict) -> tuple[str, dict]:
    """Classify a GSC URL Inspection response.

    Returns (index_status, evidence_dict). The evidence dict preserves the
    raw API values so the UI can display exactly what the API said.
    """
    result = (inspection or {}).get("inspectionResult") or {}
    index_state = result.get("indexStateResult") or {}
    coverage = (index_state.get("coverage") or {}).get("coverageState") or ""
    robots = index_state.get("robotsTxtState") or ""
    last_crawl = index_state.get("lastCrawlTime") or ""
    canonical = result.get("canonical") or None
    verdict = index_state.get("verdict") or None

    evidence = {
        "source": "Google Search Console",
        "api": "urlInspection.index.inspect",
        "coverage_state": coverage or None,
        "robots_txt_state": robots or None,
        "last_crawl_time": last_crawl or None,
        "canonical": canonical,
        "verdict": verdict,
        "checked_at": utcnow_iso(),
    }

    if coverage in _GSC_INDEXED_STATES:
        return IS_INDEXED, evidence
    if coverage in _GSC_NOT_INDEXED_STATES:
        return IS_NOT_INDEXED, evidence
    # "Pending", "Unknown", empty, or anything unexpected → UNKNOWN
    return IS_UNKNOWN, {**evidence, "reason": f"Search Console returned non-authoritative state: {coverage or 'empty'}"}


def crawl_status_from_gsc(evidence: dict) -> str:
    """If Search Console reports a last crawl time, we have crawl evidence."""
    return CS_CHECKED if evidence.get("last_crawl_time") else CS_UNKNOWN


async def apply_gsc_evidence(repos: Repos, pdf_id: int, inspection: dict) -> dict:
    """Persist Search Console evidence on our own page's PDF record."""
    status, evidence = classify_gsc_index_state(inspection)
    crawl = crawl_status_from_gsc(evidence)
    prev = await repos.pdfs.get(pdf_id)
    prev_status = (prev or {}).get("index_status") or IS_UNKNOWN

    await repos.pdfs.update(
        pdf_id,
        index_status=status,
        index_evidence=json_dumps(evidence),
        crawl_status=crawl,
        crawl_evidence=json_dumps(
            {
                "source": "Google Search Console",
                "status": crawl,
                "last_crawl_time": evidence.get("last_crawl_time"),
                "checked_at": utcnow_iso(),
            }
        ),
    )
    if status != prev_status:
        await repos.events.add(
            "INDEX_STATUS_CHANGED",
            f"Index status: {prev_status} → {status} "
            f"(source: {evidence['source']}, coverage: {evidence.get('coverage_state') or 'n/a'})",
            pdf_id=pdf_id,
            status="SUCCESS",
            evidence_type="SEARCH_CONSOLE",
            metadata={"from": prev_status, "to": status, "evidence": evidence},
        )
    else:
        await repos.events.add(
            "GSC_INSPECTED",
            f"Search Console inspection recorded (status unchanged: {status})",
            pdf_id=pdf_id,
            status="SUCCESS",
            evidence_type="SEARCH_CONSOLE",
            metadata={"evidence": evidence},
        )
    return {"index_status": status, "crawl_status": crawl, "evidence": evidence}


def third_party_note(domain: str) -> dict:
    """Standard, honest explanation for third-party PDF index status."""
    return {
        "status": IS_UNKNOWN,
        "source": None,
        "reason": (
            f"The target domain '{domain}' is not an authorized Search Console "
            "property for this application. No authoritative index evidence "
            "exists. Search visibility may be observed separately "
            "(non-authoritative)."
        ),
    }
