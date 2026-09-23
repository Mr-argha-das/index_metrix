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
from datetime import timedelta

from ..config import Settings
from ..database.repositories import Repos, effective_setting
from ..pdf.analyzer import analyze_pdf_isolated, check_signature
from ..pdf.fetcher import FetchError
from ..pdf.validator import URLValidationError, validate_url
from ..publishing.pages import create_page_for_pdf
from ..utils import json_dumps, sha256_hex, utcnow_iso, utcnow
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
CS_CHECKED = "SEARCH_ENGINE_CRAWL_EVIDENCE"
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
    if not job or job["status"] in ("COMPLETED", "DONE", "CANCELLED", "FAILED", "RUNNING"):
        return
    if job["job_type"] == "GOOGLE_INDEX_NOTIFY":
        from .indexing import process_notification
        await process_notification(manager, job_id)
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
        await repos.jobs.update(job_id, status="DONE", attempts=(job.get("attempts") or 0) + 1, next_attempt_at=None, completed_at=utcnow_iso(), error=None)
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
                await repos.pdfs.update(pdf_id, status=ST_FAILED, submission_status="VALIDATION_FAILED", error=message[:500])
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
        next_attempt_at=(utcnow() + timedelta(seconds=delay)).isoformat(),
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
    manager.schedule(job["id"], job.get("priority", 5), delay)


async def _handle_final(manager: QueueManager, job: dict, exc: PipelineFinal) -> None:
    repos = manager.repos
    manager._stats["failed"] += 1
    pdf_id = job.get("pdf_id") or None
    await repos.jobs.update(
        job["id"],
        status="FAILED",  # definitive validation failure, not a successful job
        attempts=(job.get("attempts") or 0) + 1,
        error=exc.message[:500],
        completed_at=utcnow_iso(),
    )
    if job["job_type"] == JOB_PIPELINE and pdf_id:
        await repos.pdfs.update(
            pdf_id,
            status=exc.status,
            submission_status="VALIDATION_FAILED",
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
        vurl = await asyncio.to_thread(validate_url, pdf["normalized_url"], allow_private=settings.allow_private_targets)
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
        "Fetching remote resource (technical fetch by our server)",
        pdf_id=pdf_id,
        status="RUNNING",
    )
    try:
        result = await manager.fetcher.fetch(vurl.url)
    except FetchError as exc:
        await repos.pdfs.update(pdf_id, last_checked_at=utcnow_iso(),
                                robots_check=json_dumps({"error": exc.code, "details": exc.message}) if exc.code.startswith("ROBOTS") else pdf.get("robots_check"))
        if exc.retryable:
            raise PipelineRetryable(exc.message) from exc
        if exc.status in (401, 403, 404, 410):
            raise PipelineFinal(
                f"Remote server returned HTTP {exc.status}. The resource could not be validated.",
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
        redirect_chain=json_dumps(result.redirect_chain),
        declared_content_length=result.content_length,
        external_crawl_status=pdf.get("external_crawl_status") if pdf.get("external_crawl_status") == CS_CHECKED else "FETCH_CHECKED",
        last_checked_at=result.fetched_at or utcnow_iso(),
        crawl_status=pdf.get("crawl_status") if pdf.get("crawl_status") in (CS_CHECKED, "CRAWL_CHECKED") else "FETCH_CHECKED",
        robots_check=json_dumps(result.robots_checks),
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
            f"Remote server returned HTTP {result.status}. The resource could not be validated.",
            ST_INVALID,
            classification="INVALID",
        )
    if result.status == 429:
        raise PipelineRetryable("Remote server rate-limited our request (HTTP 429).")
    if 500 <= result.status < 600:
        raise PipelineRetryable(f"Remote server error (HTTP {result.status}).")
    if result.status != 200:
        raise PipelineFinal(f"Unexpected HTTP status {result.status}.", ST_FAILED)

    from urllib.parse import unquote, urlsplit
    from ..pdf.html import extract_html, is_html_response

    analysis = None
    is_pdf = check_signature(result.content)
    expects_pdf = any(unquote(urlsplit(url).path).lower().endswith(".pdf")
                      for url in (vurl.url, result.final_url)) or result.content_type in ("application/pdf", "application/x-pdf")
    if not is_pdf and is_html_response(result.content, result.content_type):
        metadata = await asyncio.to_thread(extract_html, result.content, result.final_url)
        await repos.pdfs.update(pdf_id, html_metadata=json_dumps(metadata))
        headers = {k.lower(): v for k, v in result.headers.items()}
        noindex = any(word in headers.get("x-robots-tag", "").lower() for word in ("noindex", "none"))
        if expects_pdf or noindex or not metadata["publishable"]:
            reason = "Expected PDF signature (%PDF-) but received HTML; not publishing a misleading PDF page." if expects_pdf else metadata["rejectionReason"] or "Source X-Robots-Tag prohibits indexing."
            raise PipelineFinal(reason, ST_PDF_INVALID if expects_pdf else ST_INVALID, classification="INVALID_PDF" if expects_pdf else "HTML_REJECTED")
        await repos.pdfs.update(pdf_id, status="HTML_VALID", resource_type="HTML", submission_status="VALIDATED",
                               validated_at=utcnow_iso(), sha256=sha256_hex(result.content),
                               classification="HTML", title=metadata["title"],
                               first_page_text=metadata["excerpt"], text_length=metadata["textLength"],
                               page_count=None, author=None, subject=None, creator=None, producer=None,
                               created_date=None, modified_date=None, language=None)
        await repos.events.add("HTML_VALID", "HTML web page analyzed from source text and metadata.", pdf_id=pdf_id, status="SUCCESS")
    else:
        # ---- STEP 4: PDF signature (never trust Content-Type alone) --------------
        if not is_pdf and not expects_pdf:
            raise PipelineFinal(
                f"Unsupported source content type '{result.content_type or 'unknown'}'. "
                "Submit a public HTML web page or PDF document. No page was published.",
                ST_INVALID, classification="UNSUPPORTED_CONTENT",
            )
        if not is_pdf:
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
        analysis = await analyze_pdf_isolated(result.content)
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
            resource_type="PDF",
            html_metadata=None,
            submission_status="VALIDATED",
            validated_at=utcnow_iso(),
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
    # Publishing only establishes discovery channels. Preserve independent
    # evidence already recorded; revalidation must not erase it.
    current = await repos.pdfs.get(pdf_id)
    channels = ["reference-page"] + (["sitemap"] if sitemap_on else []) + (["rss"] if rss_on else [])
    await repos.pdfs.update(
        pdf_id, submission_status="DISCOVERY_SUBMITTED",
        discovery_status="DISCOVERED" if current.get("discovery_status") == "DISCOVERED" else DS_PENDING,
        discovery_channels=json_dumps(channels + ["internal-links"]),
        external_discovery_status="DISCOVERED" if current.get("external_discovery_status") == "DISCOVERED" else DS_PENDING,
    )
    await repos.events.add(
        "DISCOVERY_PENDING",
        "Waiting for search engines to discover the page via sitemap/RSS. "
        "Discovery is a possibility, not a guarantee.",
        pdf_id=pdf_id,
        status="PENDING",
    )

    from ..publishing.discovery import record_discovery_fallback
    await record_discovery_fallback(repos, pdf_id)

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
        external_crawl_status=pdf.get("external_crawl_status") if pdf.get("external_crawl_status") == CS_CHECKED else "FETCH_CHECKED",
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
    # A probe is FETCH_CHECKED, never search-engine evidence; preserve existing evidence.
    current = await repos.pdfs.get(pdf_id)
    if current and current.get("crawl_status") not in (CS_CHECKED, "CRAWL_CHECKED"):
        await repos.pdfs.update(pdf_id, crawl_status="FETCH_CHECKED")
    await repos.events.add(
        "FETCH_CHECKED",
        "Fetch checked by our server; this supplies no search-engine crawl evidence. "
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
