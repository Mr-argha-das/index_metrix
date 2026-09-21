"""Queue behaviour: execution, retry with backoff, stats, crash recovery."""
from __future__ import annotations

import asyncio

import pytest

from conftest import admin_client, get_csrf, wait_for


class TestQueueAPI:
    def test_queue_stats_shape(self, client):
        admin_client(client)
        r = client.get("/api/queue/stats")
        assert r.status_code == 200
        s = r.json()
        for key in ("pending", "running", "completed", "failed", "total", "concurrency"):
            assert key in s

    def test_queue_list(self, client):
        admin_client(client)
        r = client.get("/api/queue")
        assert r.status_code == 200
        assert "jobs" in r.json()

    def test_user_only_sees_own_jobs(self, client, pdf_server_url):
        from conftest import USER_EMAIL, USER_PASSWORD, login

        # user submits one pdf
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        r = client.post(
            "/api/pdfs",
            json={"url": f"{pdf_server_url}/docs/report.pdf?ownjob=1"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200
        wait_for(
            client,
            "/api/pdfs?per_page=50",
            lambda d: any(p.get("page") for p in d["items"]),
            timeout=30,
        )
        jobs = client.get("/api/queue").json()["jobs"]
        # every job visible to the USER belongs to their pdfs
        own_pdfs = {p["id"] for p in client.get("/api/pdfs").json()["items"]}
        for j in jobs:
            assert j["pdf_id"] in own_pdfs or j["pdf_id"] is None

    def test_cancel_completed_job_409(self, client):
        admin_client(client)
        jobs = client.get("/api/queue").json()["jobs"]
        done = [j for j in jobs if j["status"] == "DONE"]
        if not done:
            pytest.skip("no completed jobs yet")
        r = client.post(
            f"/api/queue/{done[0]['id']}/cancel", headers={"X-CSRF-Token": get_csrf(client)}
        )
        assert r.status_code == 409


class TestRetryBackoff:
    def test_backoff_schedule(self):
        from app.queue.manager import backoff_seconds

        assert backoff_seconds(1) == 5
        assert backoff_seconds(2) == 10
        assert backoff_seconds(3) == 20
        assert backoff_seconds(10) == 300  # capped

    def test_retryable_failure_retries_then_succeeds(self, client, pdf_server_url):
        """A server that 500s once then serves the PDF: with retries the job
        must eventually publish."""
        import aiohttp.web
        import asyncio as aio
        import socket
        import threading

        state = {"n": 0}

        def make_flaky():
            from scripts.dev_pdf_server import make_pdf_bytes

            pdf = make_pdf_bytes()

            app = aiohttp.web.Application()

            async def flaky(_req):
                state["n"] += 1
                if state["n"] == 1:
                    raise aiohttp.web.HTTPInternalServerError(text="boom")
                return aiohttp.web.Response(body=pdf, content_type="application/pdf")

            app.router.add_get("/flaky.pdf", flaky)
            return app

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        loop = aio.new_event_loop()
        runner = aiohttp.web.AppRunner(make_flaky())
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(aiohttp.web.TCPSite(runner, "127.0.0.1", port).start())
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            admin_client(client)
            r = client.post(
                "/api/pdfs",
                json={"url": f"http://127.0.0.1:{port}/flaky.pdf"},
                headers={"X-CSRF-Token": get_csrf(client)},
            )
            assert r.status_code == 200
            pdf_id = r.json()["accepted"][0]["pdf_id"]
            data = wait_for(
                client,
                f"/api/pdfs?per_page=50",
                lambda d: any(
                    p["id"] == pdf_id and p["status"] not in ("RECEIVED", "VALIDATING", "PDF_ANALYZING", "PAGE_GENERATING")
                    for p in d["items"]
                ),
                timeout=40,
            )
            pdf = next(p for p in data["items"] if p["id"] == pdf_id)
            assert pdf["status"] == "PAGE_PUBLISHED", pdf.get("error")
            detail = client.get(f"/api/pdfs/{pdf_id}").json()
            retries = [e for e in detail["events"] if e["event_type"] == "JOB_RETRY_SCHEDULED"]
            assert retries, "expected at least one retry to be recorded"
        finally:
            loop.call_soon_threadsafe(loop.stop)
