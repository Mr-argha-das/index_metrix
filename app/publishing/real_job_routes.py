"""Admin publishing and monitoring routes for operator-attested real vacancies."""
from __future__ import annotations

import io
import math
import re

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from .. import templates
from ..auth.routes import require_admin, require_admin_page
from ..integrations.google_indexing import IndexingError
from ..queue.indexing import ensure_notification
from ..utils import json_dumps, json_loads, utcnow_iso
from .pages import public_page_path, public_page_url
from .real_jobs import http_url, is_open, job_data, schema_for

router = APIRouter()

REQUIRED_IMPORT_FIELDS = {
    "title", "company", "company_url", "job_details", "description",
    "qualifications", "employment_type", "city", "region", "country",
    "date_posted", "valid_through",
}

ALIASES = {
    "job title": "title",
    "job_title": "title",
    "hiring organization": "company",
    "company website": "company_url",
    "employer website": "company_url",
    "genuine application url": "apply_url",
    "application url": "apply_url",
    "job details": "job_details",
    "job details url": "job_details",
    "full description": "description",
    "full description responsibilities": "description",
    "job description": "description",
    "qualifications experience": "qualifications",
    "employment type": "employment_type",
    "state region": "region",
    "original posting date": "date_posted",
    "closing date": "valid_through",
    "valid through": "valid_through",
}


def _normalize_header(value) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    text = re.sub(r"\s+", " ", text)
    return ALIASES.get(text, text.replace(" ", "_"))


def _clean_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return str(value).strip()


def _row_to_job(row: dict) -> dict:
    data = {_normalize_header(k): _clean_value(v) for k, v in row.items()}
    if not data.get("apply_url"):
        data["apply_url"] = data.get("job_details", "")
    data["authorized_real_vacancy"] = True
    return data


def _validate_import_shape(data: dict) -> list[str]:
    return sorted(field for field in REQUIRED_IMPORT_FIELDS if not data.get(field))


async def _create_real_job(manager, data: dict, user: dict):
    serialized = json_dumps(data)
    duplicates = await manager.repos.pages.find(
        lambda p: p.get("page_kind") == "real-job" and p.get("real_job") == serialized
    )
    if duplicates:
        return await projection(duplicates[0], manager.settings, manager.repos), True

    number = await manager.repos.settings.reserve_counter("internal_next_demo_job")
    now = utcnow_iso()
    page = await manager.repos.pages.insert(
        page_kind="real-job",
        job_number=number,
        slug=f"real-job-{number}",
        title=data.get("title", ""),
        description=data.get("description", "")[:300],
        real_job=serialized,
        job_status="OPEN",
        reviewed_by=user["id"],
        reviewed_at=now,
        indexing_revision=1,
        indexing_status="QUEUED",
        indexing_result=json_dumps({"stage": "PUBLISHED", "googleRequestMade": False}),
        gsc_index_status="UNKNOWN",
        gsc_crawl_status="UNKNOWN",
        gsc_last_checked_at=None,
        gsc_inspection="",
        page_url=manager.settings.public_base_url + f"/jobs/{number}",
        published_at=now,
        updated_at=now,
        sitemap_included=manager.settings.sitemap_enabled,
        rss_included=manager.settings.rss_enabled,
    )
    await ensure_notification(manager, page)
    await manager.repos.events.add(
        "REAL_JOB_PUBLISHED",
        "Operator-attested real vacancy published; Google notification queued, not indexed.",
        user_id=user["id"],
        metadata={
            "pageId": page["id"],
            "jobNumber": number,
            "publicUrl": page["page_url"],
            "sourceUrl": data.get("job_details") or data.get("apply_url"),
        },
    )
    return await projection(page, manager.settings, manager.repos), False


async def projection(page, settings, repos, notification_job=None):
    if notification_job is None:
        jobs = await repos.jobs.find(
            lambda j: j.get("job_type") == "GOOGLE_INDEX_NOTIFY"
            and (json_loads(j.get("payload"), {}) or {}).get("page_id") == page["id"]
        )
        latest = jobs[-1] if jobs else {}
    else:
        latest = notification_job
    gsc_evidence = json_loads(page.get("gsc_inspection"), {}) or {}
    result = json_loads(page.get("indexing_result"), {}) or {}
    job = job_data(page)
    return {
        "id": page["id"],
        "number": page["job_number"],
        "path": public_page_path(page),
        "url": public_page_url(settings.public_base_url, page),
        "publicUrl": public_page_url(settings.public_base_url, page),
        "job": job,
        "sourceUrl": http_url(job.get("job_details") or job.get("apply_url")),
        "status": "OPEN" if is_open(page) else "CLOSED",
        "publishedAt": page["published_at"],
        "updatedAt": page.get("updated_at"),
        "notificationStatus": page.get("indexing_status") or "NOT_REQUESTED",
        "notificationResult": result,
        "queue": {
            "id": latest.get("id"),
            "status": latest.get("status"),
            "attempts": latest.get("attempts") or 0,
            "maxAttempts": None if latest.get("job_type") == "GOOGLE_INDEX_NOTIFY" else (latest.get("max_attempts") or 0),
            "error": latest.get("error"),
            "nextAttemptAt": latest.get("next_attempt_at"),
            "completedAt": latest.get("completed_at"),
        },
        "gsc": {
            "indexStatus": page.get("gsc_index_status") or "UNKNOWN",
            "crawlStatus": page.get("gsc_crawl_status") or "UNKNOWN",
            "lastCheckedAt": page.get("gsc_last_checked_at"),
            "evidence": gsc_evidence,
        },
        "indexStatus": page.get("gsc_index_status") or "UNKNOWN",
        "crawlStatus": page.get("gsc_crawl_status") or "UNKNOWN",
    }
@router.get("/api/real-jobs/dashboard")
async def dashboard_real_jobs(request: Request, user=Depends(require_admin)):
    repos = request.app.state.repos
    rows = await repos.pages.find(lambda p: p.get("page_kind") == "real-job")
    notify = {}
    for job in await repos.jobs.all():
        if job.get("job_type") != "GOOGLE_INDEX_NOTIFY":
            continue
        page_id = (json_loads(job.get("payload"), {}) or {}).get("page_id")
        if page_id:
            notify[page_id] = job
    items = [await projection(p, request.app.state.settings, repos, notify.get(p["id"])) for p in reversed(rows)]
    counts = {
        "total": len(items),
        "open": sum(1 for p in items if p["status"] == "OPEN"),
        "closed": sum(1 for p in items if p["status"] == "CLOSED"),
        "queued": sum(1 for p in items if p["notificationStatus"] == "QUEUED"),
        "sent": sum(1 for p in items if p["notificationStatus"] == "ACCEPTED"),
        "failed": sum(1 for p in items if p["notificationStatus"] == "FAILED"),
        "waiting": sum(1 for p in items if p["notificationStatus"] in {"NOT_CONFIGURED", "PAUSED", "WAITING_QUOTA", "RETRY_WAITING"}),
        "indexed": sum(1 for p in items if p["indexStatus"] == "INDEXED"),
        "notIndexed": sum(1 for p in items if p["indexStatus"] == "NOT_INDEXED"),
        "unknown": sum(1 for p in items if p["indexStatus"] == "UNKNOWN"),
        "crawled": sum(1 for p in items if p["crawlStatus"] == "SEARCH_ENGINE_CRAWL_EVIDENCE"),
    }
    return {"counts": counts, "items": items[:25]}


@router.post("/api/real-jobs")
async def create_job(request: Request, payload: dict, user=Depends(require_admin)):
    manager = request.app.state.queue
    data = dict(payload or {})
    data["authorized_real_vacancy"] = data.get("authorized_real_vacancy") is True
    if not data["authorized_real_vacancy"]:
        raise HTTPException(400, "Authorization/attestation is required before publishing a real vacancy.")
    if not data.get("job_details") and data.get("apply_url"):
        data["job_details"] = data["apply_url"]
    if not data.get("apply_url") and data.get("job_details"):
        data["apply_url"] = data["job_details"]

    async with manager.indexing_lock:
        result, duplicate = await _create_real_job(manager, data, user)
    return {"duplicate": duplicate, **result}


@router.post("/api/real-jobs/import")
async def import_jobs(request: Request, file: UploadFile = File(...), authorized_bulk: bool = Form(False), user=Depends(require_admin)):
    if not authorized_bulk:
        raise HTTPException(400, "Confirm that every vacancy in this sheet is genuine and that you are authorized to publish it.")
    filename = (file.filename or "").lower()
    if not filename.endswith((".csv", ".xlsx")):
        raise HTTPException(400, "Upload a CSV or Excel sheet (.csv or .xlsx).")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Sheet is larger than 10 MB.")

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl")
    except Exception:
        raise HTTPException(400, "Could not read the sheet. Upload a valid CSV or XLSX file.") from None

    if df.empty:
        raise HTTPException(400, "The sheet contains no vacancy rows.")

    rows = []
    errors = []
    for row_number, raw_row in enumerate(df.to_dict(orient="records"), start=2):
        data = _row_to_job(raw_row)
        missing = _validate_import_shape(data)
        if missing:
            errors.append({"row": row_number, "error": "Missing fields: " + ", ".join(missing)})
            continue
        rows.append(data)

    if errors and not rows:
        return {"created": [], "duplicates": [], "errors": errors, "totalRows": len(df)}

    manager = request.app.state.queue
    created, duplicates = [], []
    async with manager.indexing_lock:
        for data in rows:
            try:
                result, duplicate = await _create_real_job(manager, data, user)
                (duplicates if duplicate else created).append(result)
            except Exception as exc:
                errors.append({"row": None, "error": "Could not publish vacancy: " + str(exc)[:300]})

    return {
        "created": created,
        "duplicates": duplicates,
        "errors": errors,
        "totalRows": len(df),
    }



def render_real_job(request, page):
    active = is_open(page)
    url = public_page_url(request.app.state.settings.public_base_url, page)
    return templates.render(
        request,
        "real_job_page.html",
        {
            "job": job_data(page),
            "page": page,
            "active": active,
            "canonical_url": url,
            "job_schema": schema_for(page, url) if active else None,
            "job_details_url": http_url(job_data(page).get("job_details") or job_data(page).get("apply_url")),
            "company_url": http_url(job_data(page).get("company_url")),
        },
    )
