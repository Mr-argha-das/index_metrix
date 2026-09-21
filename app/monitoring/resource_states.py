"""Compatibility projection for independent reference and external resources.

New fields are persisted. Legacy fields remain readable, but source fetches
never become reference-page fetches, and GSC proof never becomes external proof.
"""
from ..utils import json_loads


def unknown(value):
    return "UNKNOWN" if value in (None, "", "INDEX_UNKNOWN", "CRAWL_UNKNOWN") else value


def resource_states(row: dict, published: bool = False) -> dict:
    crawl = json_loads(row.get("crawl_evidence"), {}) or {}
    legacy_reference_crawl = (row.get("crawl_status") in ("CRAWL_CHECKED", "SEARCH_ENGINE_CRAWL_EVIDENCE")
                              and crawl.get("source") == "Google Search Console" and crawl.get("last_crawl_time"))
    return {
        "referenceSubmissionStatus": row.get("reference_submission_status") or "NOT_REQUESTED",
        "referenceSubmissionMechanism": "normal-discovery",
        "referenceSubmissionResult": json_loads(row.get("reference_submission_result"), {}) or {},
        "referenceRequestIndexingStatus": "UNSUPPORTED",
        "referenceCrawlStatus": row.get("reference_crawl_status") or ("SEARCH_ENGINE_CRAWL_EVIDENCE" if legacy_reference_crawl else "UNKNOWN"),
        "referenceCrawlEvidence": json_loads(row.get("reference_crawl_evidence"), {}) or (crawl if legacy_reference_crawl else {}),
        "referenceIndexStatus": unknown(row.get("reference_index_status") or row.get("index_status")),
        "referenceIndexEvidence": json_loads(row.get("index_evidence"), {}) or {},
        "referenceLastChecked": row.get("reference_last_checked_at"),
        "externalDiscoveryStatus": row.get("external_discovery_status") or ("DISCOVERY_PENDING" if published else "NOT_SUBMITTED"),
        "externalDiscoveryEvidence": json_loads(row.get("external_discovery_evidence"), {}) or {},
        "externalCrawlStatus": row.get("external_crawl_status") or ("FETCH_CHECKED" if row.get("http_status") else "UNKNOWN"),
        "externalCrawlEvidence": json_loads(row.get("external_crawl_evidence"), {}) or {},
        "externalIndexStatus": unknown(row.get("external_index_status") or row.get("source_index_status")),
        "externalIndexEvidence": json_loads(row.get("source_index_evidence"), {}) or {},
    }
