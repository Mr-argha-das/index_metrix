"""Schema migrations.

Feather files have a flat schema per file. Migration = adding missing
columns (or dropping deprecated ones), which :class:`TableStore.init`
performs automatically. Before any destructive change the table is backed up
into ``data/backups/``.

Use ``python scripts/restore.py --table <name> --backup <file>`` to roll back.
"""
from __future__ import annotations

import logging

from .feather_store import Database, DatabaseCorruptedError, TABLE_SCHEMAS

log = logging.getLogger("bot_indexer.migrations")

SCHEMA_VERSION = "4"


def describe_schemas() -> dict[str, dict[str, str]]:
    """Human-readable schema map for docs / the settings page."""
    return TABLE_SCHEMAS


async def migrate_all(db: Database) -> list[str]:
    """Ensure every table matches the current schema (idempotent).

    TableStore.init() already runs the add-columns migration on load; this
    function gives callers an explicit, logged entry point and verifies the
    catalog in the primary ``bot_indexer.feather`` file.
    """
    performed: list[str] = []
    for name, store in db.tables.items():
        try:
            store.init()
        except DatabaseCorruptedError:
            log.error("Migration for table '%s' failed: file corrupted", name)
            raise
        if len(store.df) or store.path.exists():
            performed.append(name)
    log.info("Schema migration complete (%s tables), version %s", len(performed), SCHEMA_VERSION)
    return performed


def backup_all(db: Database) -> list[str]:
    """Backup every table before a planned destructive change."""
    paths = []
    for store in db.tables.values():
        p = store.backup(when="pre-migration")
        if p:
            paths.append(str(p))
    return paths


async def repair_status_semantics(repos) -> None:
    """Backfill independent states, preserving existing published slugs/content.

    In particular, old 'Published'/'IndexingRequested' coverage strings are
    not indexing evidence. Keep the old evidence in the audit event.
    """
    from ..monitoring.status import classify_gsc_index_state
    from ..utils import json_dumps, json_loads

    pages = {p["pdf_id"]: p for p in await repos.pages.all()}
    for pdf in await repos.pdfs.all():
        changes = {}
        page = pages.get(pdf["id"])
        if not pdf.get("submission_status"):
            changes["submission_status"] = "DISCOVERY_SUBMITTED" if page else (
                "VALIDATION_FAILED" if pdf.get("status") in ("INVALID", "PDF_INVALID", "FAILED", "PDF_ANALYSIS_FAILED") else "RECEIVED")
        if not page and pdf.get("discovery_status") == "DISCOVERY_PENDING":
            changes["discovery_status"] = "NOT_SUBMITTED"
        if page and not pdf.get("discovery_channels"):
            changes["discovery_channels"] = json_dumps(["reference-page"] +
                (["sitemap"] if page.get("sitemap_included") else []) +
                (["rss"] if page.get("rss_included") else []))
        proof = json_loads(pdf.get("index_evidence"), {}) or {}
        if pdf.get("index_status") == "INDEXED" and proof.get("source") != "operator-confirmed":
            verdict, _ = classify_gsc_index_state({"inspectionResult": {"indexStatusResult": {
                "coverageState": proof.get("coverage_state"), "verdict": proof.get("verdict")}}})
            if verdict != "INDEXED" or proof.get("source") != "Google Search Console":
                changes["index_status"] = "INDEX_UNKNOWN"
                await repos.events.add("INDEX_EVIDENCE_CORRECTED", "Legacy status lacked independent indexing evidence; reset to UNKNOWN.",
                                       pdf_id=pdf["id"], metadata={"previousEvidence": proof})
        crawl = json_loads(pdf.get("crawl_evidence"), {}) or {}
        if pdf.get("crawl_status") == "CRAWL_CHECKED":
            changes["crawl_status"] = "SEARCH_ENGINE_CRAWL_EVIDENCE" if crawl.get("source") == "Google Search Console" and crawl.get("last_crawl_time") else "CRAWL_UNKNOWN"
        if pdf.get("crawl_status") == "CRAWL_UNKNOWN" and pdf.get("http_status"):
            changes["crawl_status"] = "FETCH_CHECKED"
        from ..monitoring.resource_states import resource_states
        projected = resource_states({**pdf, **changes}, bool(page))
        for field, key in (
            ("reference_crawl_status", "referenceCrawlStatus"),
            ("reference_index_status", "referenceIndexStatus"),
            ("external_discovery_status", "externalDiscoveryStatus"),
            ("external_crawl_status", "externalCrawlStatus"),
            ("external_index_status", "externalIndexStatus"),
        ):
            if not pdf.get(field):
                changes[field] = projected[key]
        if changes.get("index_status") == "INDEX_UNKNOWN":
            changes["reference_index_status"] = "UNKNOWN"
        if page:
            from ..publishing.discovery import REQUEST_INDEXING_REASON
            channels = json_loads(changes.get("discovery_channels") or pdf.get("discovery_channels"), []) or []
            if "internal-links" not in channels:
                channels.append("internal-links")
                changes["discovery_channels"] = json_dumps(channels)
            if not pdf.get("reference_submission_status") or pdf.get("reference_submission_status") == "NOT_REQUESTED":
                changes["reference_submission_status"] = "UNSUPPORTED"
                changes["reference_submission_result"] = json_dumps({
                    "operation": "request-indexing", "status": "UNSUPPORTED", "reason": REQUEST_INDEXING_REASON,
                    "fallback": "normal-discovery", "channels": channels, "googleRequestMade": False})
        if changes:
            await repos.pdfs.update(pdf["id"], **changes)
