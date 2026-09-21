"""In-process async job queue with retry and backoff.

Jobs persist in ``jobs.feather`` so a restart never loses queued work:
on startup, jobs left ``RUNNING`` (crashed mid-flight) are re-queued, and
``RETRY_WAITING`` jobs are rescheduled. The pipeline itself is idempotent, so
re-running a job after a crash is safe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Settings
from ..database.repositories import Repos
from ..utils import json_dumps, utcnow_iso

log = logging.getLogger("bot_indexer.queue")

# Job types
JOB_PIPELINE = "PIPELINE"        # full validate→fetch→analyze→publish pipeline
JOB_PROBE = "PROBE"              # technical server probe of the original URL
JOB_OBSERVE = "OBSERVE"          # search visibility observation (non-authoritative)
JOB_GSC_INSPECT = "GSC_INSPECT"  # Google Search Console URL inspection (own pages)

JOB_TYPES = (JOB_PIPELINE, JOB_PROBE, JOB_OBSERVE, JOB_GSC_INSPECT)

# Job statuses
JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_RETRY_WAITING = "RETRY_WAITING"
JOB_COMPLETED = "COMPLETED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"


def backoff_seconds(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


class QueueManager:
    def __init__(self, repos: Repos, settings: Settings):
        self.repos = repos
        self.settings = settings
        # set by the application lifespan once shared services exist
        self.fetcher = None
        self.gsc = None
        self.bing = None
        self.queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()  # (job_id, priority)
        self._workers: list[asyncio.Task] = []
        self._running: set[int] = set()
        self._stop = asyncio.Event()
        self._stats = {
            "enqueued": 0,
            "completed": 0,
            "failed": 0,
        }

    # -- lifecycle -----------------------------------------------------------

    @property
    def concurrency(self) -> int:
        return self.settings.indexer_concurrency

    async def start(self) -> None:
        # Crash recovery: re-queue interrupted work.
        jobs = await self.repos.jobs.all()
        for job in jobs:
            if job["status"] == JOB_RUNNING:
                await self.repos.jobs.update(job["id"], status=JOB_PENDING, error="Interrupted by server restart; re-queued.")
                await self._requeue(job["id"])
                await self.repos.events.add(
                    "JOB_REQUEUED",
                    f"Job {job['id']} ({job['job_type']}) was interrupted and re-queued",
                    pdf_id=job.get("pdf_id") or None,
                    status="INFO",
                )
            elif job["status"] == JOB_RETRY_WAITING:
                await self._requeue(job["id"])
        for _ in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker_loop(), name="queue-worker"))
        log.info("Queue started with %d workers", self.concurrency)

    async def stop(self) -> None:
        self._stop.set()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers = []
        log.info("Queue stopped")

    async def _requeue(self, job_id: int) -> None:
        job = await self.repos.jobs.get(job_id)
        if job:
            self.queue.put_nowait((job["id"], job.get("priority", 5)))

    # -- API -----------------------------------------------------------------

    async def enqueue(
        self,
        job_type: str,
        pdf_id: int | None = None,
        payload: dict | None = None,
        priority: int = 5,
        max_attempts: int | None = None,
    ) -> dict:
        if job_type not in JOB_TYPES:
            raise ValueError(f"Unknown job type: {job_type}")
        max_attempts = max_attempts if max_attempts is not None else (1 if job_type != JOB_PIPELINE else self.settings.max_retries + 1)
        job = await self.repos.jobs.insert(
            pdf_id=pdf_id,
            job_type=job_type,
            payload=json_dumps(payload) if payload else "",
            priority=int(priority),
            status=JOB_PENDING,
            attempts=0,
            max_attempts=max_attempts,
            error=None,
            started_at=None,
            completed_at=None,
            created_at=utcnow_iso(),
        )
        self._stats["enqueued"] += 1
        # small stagger so bulk submissions are spread out naturally
        if job_type == JOB_PIPELINE and (payload or {}).get("stagger_seconds"):
            try:
                stagger = float(payload["stagger_seconds"])
            except (TypeError, ValueError):
                stagger = 0.0
            asyncio.get_running_loop().call_later(stagger, lambda: self.queue.put_nowait((job["id"], priority)))
        else:
            self.queue.put_nowait((job["id"], priority))
        return job

    async def cancel(self, job_id: int) -> bool:
        job = await self.repos.jobs.get(job_id)
        if not job or job["status"] not in (JOB_PENDING, JOB_RETRY_WAITING):
            return False
        await self.repos.jobs.update(job_id, status=JOB_CANCELLED, completed_at=utcnow_iso())
        return True

    async def stats(self) -> dict:
        jobs = await self.repos.jobs.all()
        by: dict[str, int] = {}
        for j in jobs:
            by[j["status"]] = by.get(j["status"], 0) + 1
        return {
            "pending": by.get(JOB_PENDING, 0),
            "running": by.get(JOB_RUNNING, 0),
            "retrying": by.get(JOB_RETRY_WAITING, 0),
            "completed": by.get(JOB_COMPLETED, 0),
            "failed": by.get(JOB_FAILED, 0),
            "cancelled": by.get(JOB_CANCELLED, 0),
            "total": len(jobs),
            "concurrency": self.concurrency,
            "runtime": dict(self._stats),
        }

    async def list_jobs(self, limit: int = 100) -> list[dict]:
        return await self.repos.jobs.all(limit=limit, order="-id")

    # -- worker ---------------------------------------------------------------

    async def _worker_loop(self) -> None:
        from .worker import process_job

        while not self._stop.is_set():
            try:
                job_id, _priority = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await process_job(self, job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Unhandled worker error for job %s", job_id)
            finally:
                self.queue.task_done()
