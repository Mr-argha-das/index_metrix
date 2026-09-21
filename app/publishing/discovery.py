"""Official normal-discovery fallback; intentionally performs NO Google request.

Decision: Google has no generic public request-indexing API for these pages.
See GOOGLE_DISCOVERY.md (official documentation checked 2026-09-21).
Sitemaps/RSS/internal links are discovery signals, never indexing evidence.
"""
from ..utils import json_dumps, json_loads

REQUEST_INDEXING_REASON = (
    "Google does not provide a public request-indexing API for arbitrary PDF/blog "
    "reference pages. Use normal discovery via internal links, sitemap and RSS. "
    "Publication and sitemap submission do not prove crawling or indexing."
)


async def record_discovery_fallback(repos, resource_id: int) -> None:
    row = await repos.pdfs.get(resource_id)
    if not row:
        return
    channels = json_loads(row.get("discovery_channels"), []) or []
    result = {
        "operation": "request-indexing",
        "status": "UNSUPPORTED",
        "reason": REQUEST_INDEXING_REASON,
        "fallback": "normal-discovery",
        "channels": channels,
        "googleRequestMade": False,
    }
    await repos.pdfs.update(resource_id, reference_submission_status="UNSUPPORTED",
                            reference_submission_result=json_dumps(result))
