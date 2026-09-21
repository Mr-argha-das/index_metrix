"""Monitoring API + page: lifecycle evidence, technical probes, observations."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import templates
from ..auth.routes import require_user, require_user_page
from ..database.repositories import Repos
from ..queue.manager import JOB_GSC_INSPECT, JOB_OBSERVE, JOB_PROBE, QueueManager
from ..utils import json_loads
from .status import third_party_note

log = logging.getLogger("bot_indexer.monitoring.routes")

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])
page = APIRouter(tags=["monitoring-pages"])


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _queue(request: Request) -> QueueManager:
    return request.app.state.queue


def _monitoring_row(pdf: dict, request: Request) -> dict:
    index_ev = json_loads(pdf.get("index_evidence"), {}) or {}
    crawl_ev = json_loads(pdf.get("crawl_evidence"), {}) or {}
    return {
        "id": pdf["id"],
        "title": pdf.get("title") or pdf.get("normalized_url"),
        "original_url": pdf.get("original_url"),
        "source_domain": pdf.get("source_domain"),
        "status": pdf.get("status"),
        "discovery_status": pdf.get("discovery_status"),
        "crawl_status": pdf.get("crawl_status"),
        "index_status": pdf.get("index_status"),
        "index_evidence": index_ev,
        "crawl_evidence": crawl_ev,
        "third_party_note": third_party_note(pdf.get("source_domain") or ""),
        "last_probe_at": pdf.get("last_probe_at"),
        "last_probe_status": pdf.get("last_probe_status"),
        "page_url": _page_url(request, pdf["id"]),
    }


def _page_url(request: Request, pdf_id: int) -> str | None:
    # filled in by list endpoints after a batch fetch of pages
    cache = getattr(request.app.state, "_page_cache", None)
    if cache and pdf_id in cache:
        return cache[pdf_id]
    return None


async def _fill_page_cache(request: Request) -> None:
    repos = _repos(request)
    pages = await repos.pages.all()
    request.app.state._page_cache = {p["pdf_id"]: p.get("page_url") for p in pages}


def _can_access(pdf: dict, user: dict) -> bool:
    return user.get("role") == "ADMIN" or pdf.get("user_id") == user.get("id")


@router.get("")
async def api_monitoring_list(
    request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    await _fill_page_cache(request)
    pdfs = await repos.pdfs.all(order="-id")
    rows = [_monitoring_row(p, request) for p in pdfs if _can_access(p, user)]
    return {"items": rows}


@router.get("/{pdf_id}")
async def api_monitoring_detail(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    await _fill_page_cache(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if not _can_access(pdf, user):
        raise HTTPException(status_code=403, detail="You can only monitor your own submissions.")
    events = await repos.events.for_pdf(pdf_id, limit=100)
    row = _monitoring_row(pdf, request)
    row["events"] = events
    row["pdf"] = {k: v for k, v in pdf.items() if k != "first_page_text"}
    row["pdf"]["first_page_text"] = (pdf.get("first_page_text") or "")[:400]
    return {"item": row}


class ProbeIn(BaseModel):
    note: str | None = None


@router.post("/{pdf_id}/probe")
async def api_run_probe(
    pdf_id: int,
    request: Request,
    user: dict = Depends(require_user),
    payload: ProbeIn | None = None,
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if not _can_access(pdf, user):
        raise HTTPException(status_code=403, detail="You can only monitor your own submissions.")
    job = await _queue(request).enqueue(JOB_PROBE, pdf_id=pdf_id, payload={"kind": "manual"})
    return {"job_id": job["id"], "status": job["status"]}


@router.post("/{pdf_id}/observe")
async def api_run_observe(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if not _can_access(pdf, user):
        raise HTTPException(status_code=403, detail="You can only monitor your own submissions.")
    job = await _queue(request).enqueue(JOB_OBSERVE, pdf_id=pdf_id)
    return {"job_id": job["id"], "status": job["status"]}


@router.post("/{pdf_id}/inspect")
async def api_gsc_inspect(
    pdf_id: int, request: Request, user: dict = Depends(require_user)
):
    """URL Inspection via Search Console — only for OUR own pages."""
    repos = _repos(request)
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found.")
    if not _can_access(pdf, user):
        raise HTTPException(status_code=403, detail="You can only monitor your own submissions.")
    pages = await repos.pages.find(lambda p: p.get("pdf_id") == pdf_id)
    if not pages:
        raise HTTPException(status_code=400, detail="No dedicated page exists for this PDF yet.")
    job = await _queue(request).enqueue(JOB_GSC_INSPECT, pdf_id=pdf_id)
    return {"job_id": job["id"], "status": job["status"]}


@page.get("/monitoring", include_in_schema=False)
async def monitoring_page(request: Request, user: dict = Depends(require_user_page)):
    repos = _repos(request)
    await _fill_page_cache(request)
    pdfs = await repos.pdfs.all(order="-id")
    rows = [_monitoring_row(p, request) for p in pdfs if _can_access(p, user)]
    return templates.render(request, "monitoring.html", {"items": rows})
