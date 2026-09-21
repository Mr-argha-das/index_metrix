"""Authenticated INDEX MATRIX compatibility API, with explicit evidence scope.

The reference page and remote PDF are independent resources. A GSC check of
our reference page must never mark the external PDF indexed.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool

from ..auth.routes import require_admin, require_user
from ..pdf.routes import PdfIn, _submit_urls
from ..pdf.validator import URLValidationError, normalize_url
from ..utils import json_dumps, json_loads, utcnow_iso
from ..publishing.pages import public_page_url

router = APIRouter(prefix="/api/index", tags=["index"])


def status_record(pdf: dict, page: dict | None, base: str) -> dict:
    submission = pdf.get("submission_status")
    if not submission:
        submission = "DISCOVERY_SUBMITTED" if page else (
            "VALIDATION_FAILED" if pdf.get("status") in ("INVALID", "PDF_INVALID", "FAILED") else "RECEIVED")
    channels = json_loads(pdf.get("discovery_channels"), []) or []
    if not channels and page:
        channels = ["reference-page"] + (["sitemap"] if page.get("sitemap_included") else []) + (["rss"] if page.get("rss_included") else [])
    crawl = pdf.get("crawl_status") or "UNKNOWN"
    if crawl == "CRAWL_CHECKED":
        crawl = "SEARCH_ENGINE_CRAWL_EVIDENCE" if json_loads(pdf.get("crawl_evidence"), {}).get("last_crawl_time") else "UNKNOWN"
    if crawl == "CRAWL_UNKNOWN":
        crawl = "FETCH_CHECKED" if pdf.get("http_status") or pdf.get("last_probe_at") else "UNKNOWN"
    index = pdf.get("index_status") or "UNKNOWN"
    source_index = pdf.get("source_index_status") or "UNKNOWN"
    return {
        "id": pdf["id"], "url": pdf["normalized_url"], "sourceUrl": pdf["original_url"],
        "finalUrl": pdf.get("final_url"), "sourceDomain": pdf.get("source_domain"),
        "type": "PDF" if pdf.get("sha256") else "HTML" if pdf.get("html_metadata") else "UNKNOWN",
        "classification": pdf.get("classification"), "httpStatus": pdf.get("http_status"),
        "contentType": pdf.get("content_type"), "pages": pdf.get("page_count"),
        "sizeBytes": pdf.get("content_length"), "sha256": pdf.get("sha256"),
        "title": (page or {}).get("title") or pdf.get("title"),
        "description": (page or {}).get("description"),
        "textLength": pdf.get("text_length"), "excerpt": pdf.get("first_page_text"),
        "canonical": public_page_url(base, page["slug"]) if page else None,
        "referencePage": public_page_url(base, page["slug"]) if page else None,
        "referenceId": page["slug"] if page else None,
        "publishedAt": (page or {}).get("published_at"), "updatedAt": (page or pdf).get("updated_at"),
        "validatedAt": pdf.get("validated_at"), "lastChecked": pdf.get("last_checked_at") or pdf.get("last_probe_at"),
        "submissionStatus": submission,
        "discoveryStatus": pdf.get("discovery_status") if page else "NOT_SUBMITTED",
        "discoveryChannels": channels,
        "crawlStatus": crawl,
        "crawlTarget": "reference-page" if crawl == "SEARCH_ENGINE_CRAWL_EVIDENCE" else "source-url",
        "crawlEvidence": json_loads(pdf.get("crawl_evidence"), {}) or {},
        "indexStatus": "UNKNOWN" if index == "INDEX_UNKNOWN" else index,
        "indexTarget": "reference-page",
        "indexEvidence": json_loads(pdf.get("index_evidence"), {}) or {},
        "sourceIndexStatus": "UNKNOWN" if source_index == "INDEX_UNKNOWN" else source_index,
        "sourceIndexEvidence": json_loads(pdf.get("source_index_evidence"), {}) or {},
        "robotsCheck": json_loads(pdf.get("robots_check"), []) or [],
        "htmlMetadata": json_loads(pdf.get("html_metadata"), {}) or {}, "error": pdf.get("error"),
    }


@router.post("/validate", status_code=202)
async def validate(payload: PdfIn, request: Request, user: dict = Depends(require_user)):
    urls = payload.urls or ([payload.url] if payload.url else [])
    if not urls:
        raise HTTPException(400, "Provide a non-empty urls array.")
    return await _submit_urls(request, urls, user)


async def _records(request: Request, user: dict) -> list[dict]:
    repos = request.app.state.repos
    pages = {p["pdf_id"]: p for p in await repos.pages.all()}
    return [status_record(p, pages.get(p["id"]), request.app.state.settings.public_base_url)
            for p in await repos.pdfs.all(order="-id")
            if user.get("role") == "ADMIN" or p.get("user_id") == user["id"]]


def _find(records: list[dict], url: str) -> dict:
    try:
        normalized = normalize_url(url)
    except URLValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    for record in records:
        if normalized in (record["url"], record["referencePage"]):
            return record
    raise HTTPException(404, "URL not found.")


@router.get("/status")
async def status(request: Request, url: str | None = None, page: int = Query(1, ge=1),
                 per_page: int = Query(100, ge=1, le=1000), user: dict = Depends(require_user)):
    records = await _records(request, user)
    if url:
        return _find(records, url)
    summary = {
        "total": len(records), "validated": sum(bool(r["sha256"]) for r in records),
        "validationFailed": sum(r["submissionStatus"] == "VALIDATION_FAILED" for r in records),
        "referencePagesPublished": sum(bool(r["referencePage"]) for r in records),
        "discoverySubmitted": sum(r["submissionStatus"] == "DISCOVERY_SUBMITTED" for r in records),
        "discoveryPending": sum(r["discoveryStatus"] == "DISCOVERY_PENDING" for r in records),
        "discovered": sum(r["discoveryStatus"] == "DISCOVERED" for r in records),
        "fetchChecked": sum(bool(r["lastChecked"]) for r in records),
        "indexed": sum(r["indexStatus"] == "INDEXED" and bool(r["indexEvidence"]) for r in records),
        "notIndexed": sum(r["indexStatus"] == "NOT_INDEXED" for r in records),
        "unknown": sum(r["indexStatus"] == "UNKNOWN" for r in records),
    }
    return {"items": records[(page - 1) * per_page:page * per_page], "total": len(records),
            "page": page, "perPage": per_page, "summary": summary, "indexTarget": "reference-page"}


@router.get("/status/{encoded_url:path}")
async def status_url(encoded_url: str, request: Request, user: dict = Depends(require_user)):
    # ASGI already decoded the path once. Do not double-unquote URL escapes.
    return _find(await _records(request, user), encoded_url)


class EvidenceIn(BaseModel):
    url: str = Field(max_length=2048)
    indexed: StrictBool
    source: Literal["operator-confirmed"]
    details: str = Field(min_length=10, max_length=4000)


@router.post("/evidence")
async def evidence(payload: EvidenceIn, request: Request, user: dict = Depends(require_admin)):
    record = _find(await _records(request, user), payload.url)
    target = "reference-page" if normalize_url(payload.url) == record["referencePage"] else "source-url"
    if len(payload.details.strip()) < 10:
        raise HTTPException(400, "Describe the independent evidence; a fetch or sitemap receipt is not evidence.")
    proof = {"url": normalize_url(payload.url), "target": target, "source": payload.source,
             "details": payload.details.strip(), "operatorId": user["id"], "checkedAt": utcnow_iso(),
             "verification": "Operator attestation, not automatically verified by INDEX MATRIX"}
    state = "INDEXED" if payload.indexed else "NOT_INDEXED"
    fields = {"source_index_status": state, "source_index_evidence": json_dumps(proof)}
    if target == "reference-page":
        fields = {"index_status": state, "index_evidence": json_dumps(proof)}
        if payload.indexed:
            fields["discovery_status"] = "DISCOVERED"
    await request.app.state.repos.pdfs.update(record["id"], **fields)
    await request.app.state.repos.events.add("INDEPENDENT_INDEX_EVIDENCE", f"{target}: {state}",
                                           pdf_id=record["id"], user_id=user["id"],
                                           evidence_type="OPERATOR_CONFIRMED", metadata=proof)
    return {"indexStatus": state, "target": target, "evidence": proof}
