"""Job worker: executes pipeline / probe / observation / inspection jobs.

Pipeline (idempotent, safe to re-run after a crash):

    RECEIVED → VALIDATING → VALID → FETCH → VALIDATE_PDF → ANALYZE
    → CREATE_PAGE → UPDATE_SITEMAP → UPDATE_RSS → DISCOVERY_PENDING
    → (scheduled) TECHNICAL PROBE

Integrity rules enforced here:

* our server fetching a PDF is a **technical fetch**, never recorded as a
  search-engine crawl;
* index status is never derived from submission — it only changes when
  authoritative, authorized evidence is recorded (see
  :mod:`app.monitoring.status`).
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..database.repositories import Repos, effective_setting
from ..pdf.analyzer import analyze_pdf, check_signature
from ..pdf.fetcher import FetchError
from ..pdf.validator import URLValidationError, validate_url
from ..publishing.pages import create_page_for_pdf
from ..utils import json_dumps, sha256_hex, utcnow_iso
from .manager import JOB_GSC_INSPECT, JOB_OBSERVE, JOB_PIPELINE, JOB_PROBE, QueueManager, backoff_seconds

log = logging.getLogger("bot_indexer.worker")

# PDF lifecycle states
ST_RECEIVED = "RECEIVED"
ST_VALIDATING = "VALIDATING"
ST_VALID = "VALID"
ST_INVALID = "INVALID"
ST_ANALYZING = "PDF_ANALYZING"
ST_PDF_VALID = "PDF_VALID"
ST_PDF_INVALID = "PDF_INVALID"
ST_ANALYSIS_FAILED = "PDF_ANALYSIS_FAILED"
ST_PAGE_GENERATING = "PAGE_GENERATING"
ST_PAGE_PUBLISHED = "PAGE_PUBLISHED"
ST_PAGE_FAILED = "PAGE_FAILED"
ST_FAILED = "FAILED"

DS_PENDING = "DISCOVERY_PENDING"
DS_SUBMITTED = "DISCOVERY_SUBMITTED"
CS_UNKNOWN = "CRAWL_UNKNOWN"
CS_OBSERVED = "CRAWL_OBSERVED"
CS_CHECKED = "CRAWL_CHECKED"
IS_UNKNOWN = "INDEX_UNKNOWN"
IS_INDEXED = "INDEXED"
IS_NOT_INDEXED = "NOT_INDEXED"


class PipelineRetryable(Exception):
    """Transient failure — the job should be retried with backoff."""


class PipelineFinal(Exception):
    """Definitive terminal verdict for this PDF (no retry)."""

    def __init__(self, message: str, status: str, classification: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.classification = classification


async def process_job(manager: QueueManager, job_id: int) -> None:
    repos = manager.repos
    job = await repos.jobs.get(job_id)
    if not job or job["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
        return
    if job.get("attempts", 0) >= job.get("max_attempts", 1):
        await repos.jobs.update(job_id, status="FAILED", completed_at=utcnow_iso(), error="Attempt limit reached")
        return

    first_attempt = job.get("attempts", 0) == 0
    await repos.jobs.update(
        job_id,
        status="RUNNING",
        started_at=utcnow_iso() if first_attempt else job.get("started_at"),
    )
    started = asyncio.get_event_loop().time()
    try:
        jtype = job["job_type"]
        if jtype == JOB_PIPELINE:
            await run_pipeline(manager, job)
        elif jtype == JOB_PROBE:
            await run_probe(manager, job)
        elif jtype == JOB_OBSERVE:
            await run_observe(manager, job)
        elif jtype == JOB_GSC_INSPECT:
            await run_gsc_inspect(manager, job)
        else:
            raise PipelineFinal(f"Unknown job type {jtype}", ST_FAILED)

        duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        await repos.jobs.update(job_id, status="COMPLETED", completed_at=utcnow_iso(), error=None)
        manager._stats["completed"] += 1
        log.info("job_id=%s type=%s completed duration_ms=%d", job_id, jtype, duration_ms)
        if jtype == JOB_PIPELINE:
            await repos.events.add(
                "JOB_COMPLETED",
                f"Job {job_id} completed in {duration_ms} ms",
                pdf_id=job.get("pdf_id") or None,
                status="SUCCESS",
            )
    except PipelineRetryable as exc:
        await _handle_retry(manager, job, str(exc))
    except PipelineFinal as exc:
        await _handle_final(manager, job, exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("job_id=%s unhandled error", job_id)
        await _handle_retry(manager, job, f"Unexpected error: {exc}")


async def _handle_retry(manager: QueueManager, job: dict, message: str) -> None:
    repos = manager.repos
    attempts = (job.get("attempts") or 0) + 1
    pdf_id = job.get("pdf_id") or None
    if attempts >= (job.get("max_attempts") or 1):
        manager._stats["failed"] += 1
        await repos.jobs.update(
            job["id"],
            status="FAILED",
            attempts=attempts,
            error=message[:500],
            completed_at=utcnow_iso(),
        )
        if job["job_type"] == JOB_PIPELINE and pdf_id:
            pdf = await repos.pdfs.get(pdf_id)
            if pdf and pdf.get("status") not in (ST_INVALID, ST_PDF_INVALID, ST_PAGE_PUBLISHED, ST_FAILED):
                await repos.pdfs.update(pdf_id, status=ST_FAILED, error=message[:500])
            await repos.events.add(
                "JOB_FAILED",
                f"Job {job['id']} failed after {attempts} attempts: {message[:200]}",
                pdf_id=pdf_id,
                status="ERROR",
            )
        log.error("job_id=%s FAILED after %d attempts: %s", job["id"], attempts, message)
        return
    delay = backoff_seconds(attempts, base=manager.settings.retry_backoff_base)
    await repos.jobs.update(
        job["id"],
        status="RETRY_WAITING",
        attempts=attempts,
        error=message[:500],
    )
    await repos.events.add(
        "JOB_RETRY_SCHEDULED",
        f"Job {job['id']} attempt {attempts} failed ({message[:160]}); retrying in {delay:.0f}s",
        pdf_id=pdf_id,
        status="WARN",
    )
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: manager.queue.put_nowait((job["id"], job.get("priority", 5))))


async def _handle_final(manager: QueueManager, job: dict, exc: PipelineFinal) -> None:
    repos = manager.repos
    pdf_id = job.get("pdf_id") or None
    await repos.jobs.update(
        job["id"],
        status="COMPLETED",  # job finished; the *verdict* is recorded on the PDF
        attempts=(job.get("attempts") or 0) + 1,
        error=exc.message[:500],
        completed_at=utcnow_iso(),
    )
    if job["job_type"] == JOB_PIPELINE and pdf_id:
        await repos.pdfs.update(
            pdf_id,
            status=exc.status,
            error=exc.message[:500],
            **(
                {"classification": exc.classification}
                if exc.classification
                else {}
            ),
        )
        await repos.events.add(
            "JOB_FINAL",
            exc.message[:400],
            pdf_id=pdf_id,
            status="ERROR" if exc.status in (ST_INVALID, ST_PDF_INVALID, ST_FAILED) else "INFO",
        )
    log.info("job_id=%s final verdict: %s — %s", job["id"], exc.status, exc.message[:200])


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def run_pipeline(manager: QueueManager, job: dict) -> None:
    repos = manager.repos
    settings = manager.settings
    pdf_id: int = job["pdf_id"]
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise PipelineFinal("PDF record no longer exists.", ST_FAILED)

    payload = {}
    try:
        from ..utils import json_loads

        payload = json_loads(job.get("payload"), {}) or {}
    except Exception:  # noqa: BLE001
        payload = {}

    if pdf.get("status") in (ST_PAGE_PUBLISHED, DS_PENDING, DS_SUBMITTED) and not payload.get("force"):
        await repos.events.add(
            "JOB_SKIPPED",
            "Pipeline already completed; skipped (idempotent)",
            pdf_id=pdf_id,
            status="INFO",
        )
        return

    # ---- STEP 1: validate URL ---------------------------------------------
    await repos.pdfs.update(pdf_id, status=ST_VALIDATING, error=None)
    await repos.events.add(
        "URL_VALIDATING",
        f"Validating URL: {pdf['original_url']}",
        pdf_id=pdf_id,
        status="RUNNING",
    )
    try:
        vurl = validate_url(pdf["original_url"], allow_private=settings.allow_private_targets)
    except URLValidationError as exc:
        raise PipelineFinal(str(exc), ST_INVALID, classification="INVALID") from exc
    await repos.pdfs.update(pdf_id, normalized_url=vurl.url, source_domain=vurl.host)
    await repos.events.add(
        "URL_VALID",
        f"URL valid: {vurl.url} (host {vurl.host})",
        pdf_id=pdf_id,
        status="SUCCESS",
        metadata={"host": vurl.host, "resolved_ips": vurl.resolved_ips},
    )

    # ---- STEP 2: fetch remote content ---------------------------------------
    if not manager.fetcher:
        raise PipelineRetryable("Fetcher not initialised.")
    await repos.events.add(
        "FETCH_STARTED",
        "Fetching remote PDF (technical fetch by our server)",
        pdf_id=pdf_id,
        status="RUNNING",
    )
    try:
        result = await manager.fetcher.fetch(vurl.url)
    except FetchError as exc:
        if exc.retryable:
            raise PipelineRetryable(exc.message) from exc
        if exc.status in (401, 403, 404, 410):
            raise PipelineFinal(
                f"Remote server returned HTTP {exc.status}. The PDF could not be validated.",
                ST_INVALID,
                classification="INVALID",
            ) from exc
        raise PipelineFinal(exc.message, ST_FAILED) from exc

    await repos.pdfs.update(
        pdf_id,
        http_status=result.status,
        content_type=result.content_type,
        content_length=len(result.content),
        final_url=result.final_url,
    )
    await repos.events.add(
        "FETCH_OK",
        f"Fetched {len(result.content)} bytes (HTTP {result.status}, {result.content_type or 'unknown type'}, {result.response_time_ms} ms)",
        pdf_id=pdf_id,
        status="SUCCESS",
        evidence_type="TECHNICAL_FETCH",
        metadata={
            "status": result.status,
            "content_type": result.content_type,
            "bytes": len(result.content),
            "response_time_ms": result.response_time_ms,
            "final_url": result.final_url,
            "redirects": result.redirect_chain,
        },
    )

    # ---- STEP 3: HTTP status -------------------------------------------------
    if result.status in (401, 403, 404, 410):
        raise PipelineFinal(
            f"Remote server returned HTTP {result.status}. The PDF could not be validated.",
            ST_INVALID,
            classification="INVALID",
        )
    if result.status == 429:
        raise PipelineRetryable("Remote server rate-limited our request (HTTP 429).")
    if 500 <= result.status < 600:
        raise PipelineRetryable(f"Remote server error (HTTP {result.status}).")
    if not (200 <= result.status < 300):
        raise PipelineFinal(f"Unexpected HTTP status {result.status}.", ST_FAILED)

    # ---- STEP 4: PDF signature (never trust Content-Type alone) --------------
    if not check_signature(result.content):
        raise PipelineFinal(
            f"Content mismatch: HTTP {result.status} with type "
            f"'{result.content_type or 'none'}' but the body does not contain "
            "the PDF signature (%PDF-). Not a PDF.",
            ST_PDF_INVALID,
            classification="INVALID_PDF",
        )

    # ---- STEP 5: analysis ------------------------------------------------------
    await repos.pdfs.update(pdf_id, status=ST_ANALYZING)
    await repos.events.add(
        "PDF_ANALYZING",
        "Analyzing PDF structure and metadata",
        pdf_id=pdf_id,
        status="RUNNING",
    )
    analysis = await asyncio.to_thread(analyze_pdf, result.content)
    digest = sha256_hex(result.content)

    if not analysis.ok:
        if analysis.classification == "INVALID_PDF":
            raise PipelineFinal(analysis.error or "Invalid PDF.", ST_PDF_INVALID, classification="INVALID_PDF")
        raise PipelineRetryable(analysis.error or "PDF analysis failed.")

    # duplicate content detection (same file from a different URL)
    dupes = await repos.pdfs.find(lambda r: r.get("sha256") == digest and r.get("id") != pdf_id)
    if dupes:
        await repos.events.add(
            "SAME_PDF_CONTENT",
            f"Identical PDF content already known as PDF #{dupes[0]['id']} ({dupes[0]['normalized_url']})",
            pdf_id=pdf_id,
            status="WARN",
            metadata={"other_pdf_id": dupes[0]["id"], "sha256": digest},
        )

    await repos.pdfs.update(
        pdf_id,
        status=ST_PDF_VALID,
        sha256=digest,
        classification=analysis.classification,
        title=analysis.title,
        author=analysis.author,
        subject=analysis.subject,
        creator=analysis.creator,
        producer=analysis.producer,
        created_date=analysis.creation_date,
        modified_date=analysis.modification_date,
        text_length=analysis.text_length,
        first_page_text=analysis.first_page_text,
        language=analysis.language,
        page_count=analysis.page_count,
    )
    await repos.events.add(
        "PDF_VALID",
        f"PDF valid: {analysis.page_count} page(s), {analysis.classification}, "
        f"{len(result.content)} bytes, sha256={digest[:12]}…",
        pdf_id=pdf_id,
        status="SUCCESS",
        evidence_type="TECHNICAL_ANALYSIS",
        metadata={
            "classification": analysis.classification,
            "page_count": analysis.page_count,
            "title": analysis.title,
            "sha256": digest,
        },
    )
    await repos.events.add(
        "PDF_ANALYZED",
        f"Analysis complete: {analysis.classification}, text_chars={analysis.text_length}, language≈{analysis.language}",
        pdf_id=pdf_id,
        status="SUCCESS",
    )

    # ---- STEP 6: dedicated page ------------------------------------------------
    await repos.pdfs.update(pdf_id, status=ST_PAGE_GENERATING)
    await repos.events.add("PAGE_GENERATING", "Generating dedicated page", pdf_id=pdf_id, status="RUNNING")
    try:
        page = await create_page_for_pdf(repos, settings, await repos.pdfs.get(pdf_id), analysis)
    except Exception as exc:  # noqa: BLE001
        log.exception("page generation failed")
        await repos.pdfs.update(pdf_id, status=ST_PAGE_FAILED)
        raise PipelineFinal(f"Page generation failed: {exc}", ST_PAGE_FAILED) from exc

    await repos.pdfs.update(pdf_id, status=ST_PAGE_PUBLISHED)
    await repos.events.add(
        "PAGE_PUBLISHED",
        f"Dedicated page live at {page['page_url']}",
        pdf_id=pdf_id,
        status="SUCCESS",
    )

    # ---- STEP 7: sitemap + RSS ---------------------------------------------------
    sitemap_on = bool(await _effective(manager, "sitemap_enabled"))
    rss_on = bool(await _effective(manager, "rss_enabled"))
    if sitemap_on:
        await repos.events.add(
            "SITEMAP_UPDATED",
            f"Page added to sitemap.xml ({page['page_url']})",
            pdf_id=pdf_id,
            status="SUCCESS",
        )
    if rss_on:
        await repos.events.add(
            "RSS_UPDATED",
            f"Page added to rss.xml ({page['page_url']})",
            pdf_id=pdf_id,
            status="SUCCESS",
        )

    # ---- STEP 8: discovery pending ---------------------------------------------
    await repos.pdfs.update(
        pdf_id,
        discovery_status=DS_PENDING,
        crawl_status=CS_UNKNOWN,
        index_status=IS_UNKNOWN,
        index_evidence=json_dumps(
            {"status": IS_UNKNOWN, "reason": "No authoritative evidence recorded yet."}
        ),
        crawl_evidence=json_dumps(
            {"status": CS_UNKNOWN, "reason": "No crawl evidence recorded yet. "
             "Our server fetches are technical probes, not search-engine crawls."}
        ),
    )
    await repos.events.add(
        "DISCOVERY_PENDING",
        "Waiting for search engines to discover the page via sitemap/RSS. "
        "Discovery is a possibility, not a guarantee.",
        pdf_id=pdf_id,
        status="PENDING",
    )

    # schedule the initial technical probe (honest label, not a "Google crawl")
    await manager.enqueue(JOB_PROBE, pdf_id=pdf_id, payload={"kind": "initial"})


async def _effective(manager: QueueManager, key: str):
    try:
        return await effective_setting(manager.repos.db, key)
    except Exception:  # noqa: BLE001
        return getattr(manager.settings, key)


# ---------------------------------------------------------------------------
# Technical probe
# ---------------------------------------------------------------------------


async def run_probe(manager: QueueManager, job: dict) -> None:
    repos = manager.repos
    pdf_id = job["pdf_id"]
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise PipelineFinal("PDF record no longer exists.", ST_FAILED)
    if not manager.fetcher:
        raise PipelineRetryable("Fetcher not initialised.")

    await repos.events.add(
        "PROBE_STARTED",
        "Technical Server Probe: HEAD/GET against the original URL by our server",
        pdf_id=pdf_id,
        status="RUNNING",
    )
    try:
        result = await manager.fetcher.fetch(
            pdf["normalized_url"] or pdf["original_url"], probe_only=True
        )
    except FetchError as exc:
        if exc.retryable:
            raise PipelineRetryable(exc.message) from exc
        await repos.pdfs.update(
            pdf_id,
            last_probe_at=utcnow_iso(),
            last_probe_status=f"ERROR:{exc.code}",
        )
        await repos.events.add(
            "TECHNICAL_PROBE",
            f"Technical Server Probe failed: {exc.message}",
            pdf_id=pdf_id,
            status="ERROR",
            evidence_type="TECHNICAL_PROBE",
            metadata={"code": exc.code, "status": exc.status},
        )
        return

    await repos.pdfs.update(
        pdf_id,
        last_probe_at=utcnow_iso(),
        last_probe_status=str(result.status),
    )
    await repos.events.add(
        "TECHNICAL_PROBE",
        (
            "Technical Server Probe: HTTP "
            f"{result.status}, {result.content_type or 'unknown type'}, "
            f"{result.response_time_ms} ms, final={result.final_url}. "
            "This is a fetch by OUR server — it is NOT evidence of any "
            "search-engine crawl."
        ),
        pdf_id=pdf_id,
        status="SUCCESS",
        evidence_type="TECHNICAL_PROBE",
        metadata={
            "label": "Technical Server Probe",
            "user_agent": manager.settings.fetch_user_agent,
            "status": result.status,
            "content_type": result.content_type,
            "response_time_ms": result.response_time_ms,
            "final_url": result.final_url,
            "redirects": result.redirect_chain,
            "checked_at": utcnow_iso(),
        },
    )
    # Crawl status stays CRAWL_UNKNOWN: a technical probe is not a crawl.
    await repos.pdfs.update(pdf_id, crawl_status=CS_UNKNOWN)
    await repos.events.add(
        "CRAWL_UNKNOWN",
        "Crawl status remains UNKNOWN — no search-engine crawl evidence exists. "
        "A technical probe must never be reported as a crawl.",
        pdf_id=pdf_id,
        status="INFO",
        evidence_type="NONE",
    )


# ---------------------------------------------------------------------------
# Search visibility observation (non-authoritative)
# ---------------------------------------------------------------------------


async def run_observe(manager: QueueManager, job: dict) -> None:
    from ..monitoring.observation import run_observation

    repos = manager.repos
    settings = manager.settings
    try:
        enabled = bool(await effective_setting(repos.db, "observation_enabled"))
    except Exception:  # noqa: BLE001
        enabled = settings.observation_enabled
    if not enabled:
        pdf_id = job.get("pdf_id")
        if pdf_id:
            await repos.events.add(
                "OBSERVATION_SKIPPED",
                "Search visibility observation is disabled in settings.",
                pdf_id=pdf_id,
                status="INFO",
            )
        return
    pdf_id = job["pdf_id"]
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise PipelineFinal("PDF record no longer exists.", ST_FAILED)
    result = await run_observation(manager, pdf)
    await repos.events.add(
        "OBSERVATION",
        result["message"],
        pdf_id=pdf_id,
        status="INFO",
        evidence_type="SEARCH_OBSERVATION",
        metadata=result["metadata"],
    )


# ---------------------------------------------------------------------------
# Google Search Console inspection (authorized properties only)
# ---------------------------------------------------------------------------


async def run_gsc_inspect(manager: QueueManager, job: dict) -> None:
    from ..monitoring.status import apply_gsc_evidence

    repos = manager.repos
    pdf_id = job["pdf_id"]
    pdf = await repos.pdfs.get(pdf_id)
    if not pdf:
        raise PipelineFinal("PDF record no longer exists.", ST_FAILED)
    if manager.gsc is None:
        await repos.events.add(
            "GSC_NOT_CONFIGURED",
            "Google Search Console is not configured (no service account). "
            "Index status stays UNKNOWN.",
            pdf_id=pdf_id,
            status="WARN",
        )
        return
    page_rows = await repos.pages.find(lambda p: p.get("pdf_id") == pdf_id)
    if not page_rows:
        await repos.events.add(
            "GSC_NO_PAGE",
            "No dedicated page exists to inspect.",
            pdf_id=pdf_id,
            status="WARN",
        )
        return
    result = await manager.gsc.inspect_own_page(page_rows[0]["page_url"])
    if result.get("error"):
        await repos.events.add(
            "GSC_INSPECT_ERROR",
            f"Search Console inspection failed: {result['error']} — index status stays UNKNOWN.",
            pdf_id=pdf_id,
            status="ERROR",
            evidence_type="SEARCH_CONSOLE",
            metadata=result,
        )
        return
    await apply_gsc_evidence(repos, pdf_id, result)
