"""PDF URL submission, listing, detail, retry, delete, re-analyze."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import templates
from ..auth.routes import require_user, require_user_page
from ..config import Settings
from ..database.repositories import Repos
from ..queue.manager import JOB_PIPELINE, QueueManager
from ..utils import json_loads, sha256_hex, utcnow_iso
from .validator import URLValidationError, normalize_url, validate_url

log = logging.getLogger("bot_indexer.pdf.routes")

router = APIRouter(prefix="/api/pdfs", tags=["pdfs"])
pages = APIRouter(tags=["pdf-pages"])

MAX_BULK_URLS = 10000


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _queue(request: Request) -> QueueManager:
    return request.app.state.queue


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ---------------------------------------------------------------------------
# Submission helpers
# ---------------------------------------------------------------------------


def _parse_url_text(text: str) -> list[str]:
    """Split pasted input: one URL per line, and/or comma separated."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line and line.count(",") <= 20:
            for part in line.split(","):
                part = part.strip()
                if part:
                    out.append(part)
        else:
            out.append(line)
    return out


async def _submit_urls(
    request: Request, urls: list[str], user: dict
) -> dict:
    from urllib.parse import urlsplit

    if len(urls) > MAX_BULK_URLS:
        raise HTTPException(status_code=400, detail=f"Max {MAX_BULK_URLS} URLs per batch.")
    repos, queue = _repos(request), _queue(request)
    accepted, duplicates, invalid = [], [], []
    # Serializes normalized-URL uniqueness across concurrent intake requests.
    # DNS/HTTP are deliberately deferred to workers, never performed here.
    async with queue.intake_lock:
        known = {p["url_hash"]: p for p in await repos.pdfs.all()}
        pending = {}
        now = utcnow_iso()
        for raw in urls:
            try:
                normalized = normalize_url(raw)
            except URLValidationError as exc:
                invalid.append({"url": raw[:500], "reason": str(exc)})
                continue
            digest = sha256_hex(normalized)
            if digest in known or digest in pending:
                existing = known.get(digest, {})
                item = {"url": normalized, "status": "DUPLICATE"}
                if existing and (user.get("role") == "ADMIN" or existing.get("user_id") == user["id"]):
                    item["existing_pdf_id"] = existing["id"]
                duplicates.append(item)
                continue
            pending[digest] = dict(
                user_id=user["id"], original_url=normalized, normalized_url=normalized,
                url_hash=digest, source_domain=urlsplit(normalized).hostname,
                status="RECEIVED", submission_status="RECEIVED",
                discovery_status="NOT_SUBMITTED", discovery_channels="[]",
                crawl_status="CRAWL_UNKNOWN", index_status="INDEX_UNKNOWN",
                source_index_status="INDEX_UNKNOWN", index_evidence="", crawl_evidence="",
                reference_submission_status="NOT_REQUESTED", reference_crawl_status="UNKNOWN",
                reference_index_status="UNKNOWN", external_discovery_status="NOT_SUBMITTED",
                external_crawl_status="UNKNOWN", external_index_status="UNKNOWN",
                created_at=now, updated_at=now,
            )
        pdfs = await repos.pdfs.insert_many(list(pending.values()))
        jobs = await queue.enqueue_pipelines([p["id"] for p in pdfs])
        for pdf, job in zip(pdfs, jobs):
            accepted.append({"url": pdf["normalized_url"], "pdf_id": pdf["id"],
                             "job_id": job["id"], "status": "PENDING"})
        by_url = {p["normalized_url"]: p["id"] for p in pdfs}
        for duplicate in duplicates:
            if duplicate["url"] in by_url:
                duplicate["existing_pdf_id"] = by_url[duplicate["url"]]
    return {"accepted": accepted, "duplicates": duplicates, "invalid": invalid}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class PdfIn(BaseModel):
    url: str | None = None
    urls: list[str] | None = Field(default=None, max_length=MAX_BULK_URLS)


@router.post("")
async def api_submit_pdf(
    payload: PdfIn, request: Request, user: dict = Depends(require_user)
):
    urls = payload.urls or ([payload.url] if payload.url else [])
    if not urls:
        raise HTTPException(status_code=400, detail="Provide 'url' or a non-empty 'urls' list.")
    result = await _submit_urls(request, urls, user)
    if not result["accepted"] and not result["duplicates"]:
        raise HTTPException(status_code=422, detail={"message": "No URLs could be accepted.", **result})
    return result


@router.post("/bulk")
async def api_bulk_submit(
    payload: PdfIn, request: Request, user: dict = Depends(require_user)
):
    urls = payload.urls or ([payload.url] if payload.url else [])
    if not urls:
        raise HTTPException(status_code=400, detail="Provide a non-empty 'urls' list.")
    return await _submit_urls(request, urls, user)


class PdfFileIn(BaseModel):
    pass


@router.post("/file")
async def api_submit_file(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    settings = _settings(request)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="Uploaded file is too large.")
    filename = (file.filename or "").lower()
    if filename and not (filename.endswith((".txt", ".csv"))):
        raise HTTPException(status_code=400, detail="Only .txt or .csv files are accepted.")
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Could not decode file as text.")
    urls = _parse_url_text(text)
    if not urls:
        raise HTTPException(status_code=422, detail="No URLs found in the file.")
    return await _submit_urls(request, urls, user)


@router.get("")
async def api_list_pdfs(
    request: Request,
    user: dict = Depends(require_user),
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = 25,
):
    repos = _repos(request)
    per_page = max(1, min(100, per_page))
    pdfs = await repos.pdfs.all(order="-id")
    if user.get("role") != "ADMIN":
        pdfs = [p for p in pdfs if p.get("user_id") == user.get("id")]
    if status:
        pdfs = [p for p in pdfs if p.get("status") == status]
    if q:
        needle = q.lower()
        pdfs = [
            p
            for p in pdfs
            if needle in (p.get("original_url") or "").lower()
            or needle in (p.get("source_domain") or "").lower()
            or needle in (p.get("title") or "").lower()
        ]
    total = len(pdfs)
    start = (page - 1) * per_page
    items = pdfs[start : start + per_page]
    # attach page URLs
    pages = await repos.pages.all()
    page_by_pdf = {p["pdf_id"]: p for p in pages}
    for item in items:
        item["page"] = page_by_pdf.get(item["id"])
        from ..monitoring.resource_states import resource_states
        item["states"] = resource_states(item, bool(item["page"]))
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{pdf_id}")
async def api_pdf_detail(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if user.get("role") != "ADMIN" and pdf.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="You can only view your own submissions.")
    page_row = await repos.pages.find(lambda p: p.get("pdf_id") == pdf_id)
    events = await repos.events.for_pdf(pdf_id, limit=100)
    index_ev = json_loads(pdf.get("index_evidence"), {}) or {}
    crawl_ev = json_loads(pdf.get("crawl_evidence"), {}) or {}
    return {
        "pdf": pdf,
        "page": page_row[0] if page_row else None,
        "events": events,
        "index_evidence": index_ev,
        "crawl_evidence": crawl_ev,
    }


@router.post("/{pdf_id}/retry")
async def api_pdf_retry(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if user.get("role") != "ADMIN" and pdf.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="You can only retry your own submissions.")
    if pdf.get("status") in ("VALIDATING", "PDF_ANALYZING", "PAGE_GENERATING"):
        raise HTTPException(status_code=409, detail="This URL is already processing.")
    job = await _queue(request).enqueue(JOB_PIPELINE, pdf_id=pdf_id, payload={"force": True})
    await repos.pdfs.update(pdf_id, status="RECEIVED", error=None)
    await repos.events.add("RETRY_REQUESTED", f"Manual retry requested for PDF #{pdf_id}", pdf_id=pdf_id, user_id=user.get("id"), status="RUNNING")
    return {"job_id": job["id"], "status": "QUEUED"}


@router.post("/{pdf_id}/analyze")
async def api_pdf_analyze(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if user.get("role") != "ADMIN" and pdf.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="You can only analyze your own submissions.")
    job = await _queue(request).enqueue(JOB_PIPELINE, pdf_id=pdf_id, payload={"force": True, "kind": "reanalyze"})
    await repos.events.add("REANALYZE_REQUESTED", f"Re-analysis requested for PDF #{pdf_id}", pdf_id=pdf_id, status="RUNNING")
    return {"job_id": job["id"], "status": "QUEUED"}


@router.delete("/{pdf_id}")
async def api_pdf_delete(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if user.get("role") != "ADMIN" and pdf.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="You can only delete your own submissions.")
    # remove the dedicated page (this removes it from sitemap + RSS automatically)
    pages = await repos.pages.find(lambda p: p.get("pdf_id") == pdf_id)
    for p in pages:
        await repos.pages.delete(p["id"])
    ok = await repos.pdfs.delete(pdf_id)
    await repos.events.add(
        "PDF_DELETED",
        f"PDF #{pdf_id} ({pdf.get('normalized_url')}) and its page removed; page no longer in sitemap/RSS",
        pdf_id=pdf_id,
        user_id=user.get("id"),
        status="INFO",
    )
    return {"ok": bool(ok)}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@pages.get("/submit", include_in_schema=False)
async def submit_page(request: Request, user: dict = Depends(require_user_page)):
    return templates.render(request, "pdf_submit.html")


@pages.get("/pdfs/{pdf_id}", include_in_schema=False)
async def pdf_detail_page(
    pdf_id: int, request: Request, user: dict = Depends(require_user_page)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if user.get("role") != "ADMIN" and pdf.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="You can only view your own submissions.")
    page_row = await repos.pages.find(lambda p: p.get("pdf_id") == pdf_id)
    events = await repos.events.for_pdf(pdf_id, limit=200)
    index_ev = json_loads(pdf.get("index_evidence"), {}) or {}
    crawl_ev = json_loads(pdf.get("crawl_evidence"), {}) or {}
    return templates.render(
        request,
        "pdf_detail.html",
        {"pdf": pdf, "page": page_row[0] if page_row else None, "events": events, "index_evidence": index_ev, "crawl_evidence": crawl_ev},
    )


@pages.get("/urls", include_in_schema=False)
async def urls_page(request: Request, user: dict = Depends(require_user_page)):
    return templates.render(request, "urls.html")
