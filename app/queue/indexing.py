"""Persistent, deduplicated Indexing API notifications in the existing queue.

At-least-once delivery: a crash after Google accepts but before the local write
can repeat a notification. Neither accepted requests nor page fetches mean indexed.
All callers of ensure_notification hold manager.indexing_lock.
"""
import time
from datetime import timedelta

import httpx

from ..integrations.google_indexing import GoogleIndexingClient, IndexingError, load_credentials, credential_path
from ..publishing.pages import public_page_url
from ..publishing.real_jobs import is_open, job_data
from ..utils import json_dumps, json_loads, parse_iso, utcnow, utcnow_iso
from .manager import JOB_INDEX_NOTIFY, backoff_seconds

ENABLED_KEY = "internal_google_indexing_enabled"
QUOTA_KEY = "internal_google_indexing_attempts"


async def enabled(manager):
    row = await manager.repos.settings.get_key(ENABLED_KEY)
    return bool(row and row["value"] == "true")


def payload_for(manager, page):
    kind = "URL_UPDATED" if is_open(page) else "URL_DELETED"
    return {"page_id": page["id"], "revision": page.get("indexing_revision") or 1,
            "type": kind, "url": public_page_url(manager.settings.public_base_url, page)}


async def ensure_notification(manager, page, retry=False):
    if page.get("page_kind") != "real-job" or not page.get("reviewed_by"):
        raise IndexingError("Only operator-attested real jobs are eligible; fictional demos and source URLs are excluded.")
    payload = payload_for(manager, page)
    existing = await manager.repos.jobs.find(lambda j: j["job_type"] == JOB_INDEX_NOTIFY and json_loads(j.get("payload"), {}) == payload)
    if existing:
        job = existing[-1]
        if retry and job["status"] in ("FAILED", "CANCELLED"):
            await manager.repos.jobs.update(job["id"], status="PENDING", attempts=0, error=None, next_attempt_at=None, completed_at=None)
            await manager.repos.pages.update(page["id"], touch=False, indexing_status="QUEUED")
            manager.schedule(job["id"], 5)
        return await manager.repos.jobs.get(job["id"])
    job = await manager.enqueue(JOB_INDEX_NOTIFY, payload=payload, max_attempts=4)
    await manager.repos.pages.update(page["id"], touch=False, indexing_status="QUEUED", indexing_result=json_dumps({"googleRequestMade": False, "type": payload["type"], "url": payload["url"]}))
    return job


async def reconcile(manager):
    """Recover page→queue crash windows, expire vacancies, and retain stable URLs."""
    async with manager.indexing_lock:
        for page in await manager.repos.pages.all():
            if page.get("page_kind") != "real-job":
                continue
            if page.get("job_status") == "OPEN" and not is_open(page):
                page = await manager.repos.pages.update(page["id"], job_status="CLOSED", indexing_revision=(page.get("indexing_revision") or 1) + 1,
                                                       updated_at=utcnow_iso(), sitemap_included=False, rss_included=False)

            # Migrate legacy indexing failures from the old finite-retry
            # behavior into the new automatic retry loop. The current worker
            # only reaches FAILED for non-retryable errors.
            failed = await manager.repos.jobs.find(
                lambda j: j.get("job_type") == JOB_INDEX_NOTIFY
                and j.get("status") == "FAILED"
                and (json_loads(j.get("payload"), {}) or {}).get("page_id") == page["id"]
            )
            for old_job in failed:
                old_result = json_loads(page.get("indexing_result"), {}) or {}
                http_status = old_result.get("httpStatus")
                if old_result.get("retryable") is True or http_status in (408, 429) or (isinstance(http_status, int) and http_status >= 500) or not old_result:
                    await manager.repos.jobs.update(
                        old_job["id"],
                        status="RETRY_WAITING",
                        next_attempt_at=utcnow_iso(),
                        completed_at=None,
                        error="Migrated to automatic retry loop.",
                    )
                    await manager.repos.pages.update(page["id"], touch=False, indexing_status="RETRY_WAITING")
                    manager.schedule(old_job["id"], 5)

            await ensure_notification(manager, page)


async def wake_waiting(manager):
    # Caller holds indexing_lock, so credential replacement cannot race a send.
    for job in await manager.repos.jobs.all():
        if job["job_type"] == JOB_INDEX_NOTIFY and job["status"] == "RETRY_WAITING" and job.get("error") in ("NOT_CONFIGURED", "PAUSED"):
            await manager.repos.jobs.update(job["id"], status="PENDING", next_attempt_at=None)
            manager.schedule(job["id"], 5)


async def quota_delay(manager, now):
    row = await manager.repos.settings.get_key(QUOTA_KEY)
    ledger = json_loads(row["value"], []) if row else []
    ledger = sorted(float(t) for t in ledger if float(t) > now - 86400)
    day_limit = manager.settings.google_indexing_daily_limit
    minute_limit = manager.settings.google_indexing_minute_limit
    delay = max(0, ledger[-day_limit] + 86400 - now) if len(ledger) >= day_limit else 0
    recent = [t for t in ledger if t > now - 60]
    if len(recent) >= minute_limit:
        delay = max(delay, recent[-minute_limit] + 60 - now)
    if not delay:
        # Reserve BEFORE the network call. Crashes consume a slot conservatively.
        await manager.repos.settings.set_key(QUOTA_KEY, json_dumps(ledger + [now]))
    return delay


async def defer(manager, job, page, status, delay, result=None, attempts=None):
    await manager.repos.jobs.update(job["id"], status="RETRY_WAITING", next_attempt_at=(utcnow() + timedelta(seconds=delay)).isoformat(),
                                   error=status, **({"attempts": attempts} if attempts is not None else {}))
    await manager.repos.pages.update(page["id"], touch=False, indexing_status=status, **({"indexing_result": json_dumps(result)} if result else {}))
    manager.schedule(job["id"], 5, delay)


async def process_notification(manager, job_id):
    async with manager.indexing_lock:
        repos = manager.repos
        job = await repos.jobs.get(job_id)
        if not job or job["status"] not in ("PENDING", "RETRY_WAITING"):
            return
        due = parse_iso(job.get("next_attempt_at"))
        if due and due > utcnow():
            return
        payload = json_loads(job.get("payload"), {})
        page = await repos.pages.get(payload.get("page_id", 0))
        if not page or page.get("page_kind") != "real-job" or payload != payload_for(manager, page):
            await repos.jobs.update(job_id, status="CANCELLED", completed_at=utcnow_iso(), error="Ineligible, changed, expired or superseded page. No request sent.")
            return
        attempted = job.get("attempts") or 0
        result = {"googleRequestMade": False, "url": payload["url"], "type": payload["type"], "checkedAt": utcnow_iso()}
        try:
            # Real-job indexing is intentionally not bounded by max_attempts.
            # Transient Google/network/quota failures remain in RETRY_WAITING
            # and are retried automatically with bounded exponential backoff.
            if not page.get("reviewed_by") or job_data(page).get("authorized_real_vacancy") is not True:
                raise IndexingError("Vacancy has not been attested by an authorized operator.")
            if not credential_path(manager.settings).exists():
                await defer(manager, job, page, "NOT_CONFIGURED", 300)
                return
            if not await enabled(manager):
                await defer(manager, job, page, "PAUSED", 300)
                return
            credentials = load_credentials(manager.settings)
            delay = await quota_delay(manager, time.time())
            if delay:
                await defer(manager, job, page, "WAITING_QUOTA", delay + 1)
                return
            attempted += 1
            await repos.jobs.update(job_id, status="RUNNING", started_at=utcnow_iso(), attempts=attempted, next_attempt_at=None)
            result.update(await GoogleIndexingClient(credentials, manager.settings.public_base_url).publish(payload["url"], payload["type"]))
        except Exception as exc:
            # Sanitize ALL errors, including HTTP/JWT/parser errors; never echo credentials.
            if isinstance(exc, IndexingError):
                message, retryable, status, delay, sent = str(exc), exc.retryable, exc.status, exc.retry_after, exc.sent
            else:
                message, retryable, status, delay, sent = "Indexing setup or network failure. Check configuration and retry.", isinstance(exc, httpx.HTTPError), None, 0, False
            result.update(message=message, httpStatus=status, googleRequestMade=sent)
            if retryable:
                await defer(
                    manager,
                    job,
                    page,
                    "RETRY_WAITING",
                    max(delay, backoff_seconds(max(1, attempted))),
                    result,
                    attempted,
                )
            else:
                await repos.pages.update(page["id"], touch=False, indexing_status="FAILED", indexing_result=json_dumps(result))
                await repos.jobs.update(job_id, status="FAILED", attempts=attempted, error=message, completed_at=utcnow_iso())
                manager._stats["failed"] += 1
            return
        # Persistence failures after remote acceptance must leave interrupted work
        # recoverable, not relabel an accepted request as a Google rejection.
        await repos.pages.update(page["id"], touch=False, indexing_status="ACCEPTED", indexing_result=json_dumps(result))
        await repos.jobs.update(job_id, status="DONE", completed_at=utcnow_iso(), error=None)
        if payload["type"] == "URL_UPDATED":
            # Queue a separate evidence-only Search Console inspection. This
            # never changes ACCEPTED into INDEXED unless GSC supplies evidence.
            await manager.enqueue("GSC_INSPECT", payload={"page_id": page["id"]}, max_attempts=2)
        manager._stats["completed"] += 1
        await repos.events.add("GOOGLE_NOTIFICATION_ACCEPTED", "Indexing notification accepted; index status remains UNKNOWN.", user_id=page["reviewed_by"], metadata={"pageId": page["id"], "type": payload["type"]})
