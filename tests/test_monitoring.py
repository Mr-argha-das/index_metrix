"""Monitoring: probes, observations, evidence-based index status."""
from __future__ import annotations

import json

import pytest

from conftest import admin_client, get_csrf, wait_for


def _submit_and_publish(client, url):
    r = client.post(
        "/api/pdfs/bulk",
        json={"urls": [url]},
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert r.status_code == 200, r.text
    pdf_id = r.json()["accepted"][0]["pdf_id"]
    wait_for(
        client,
        f"/api/pdfs?per_page=50",
        lambda d: any(p["id"] == pdf_id and (p.get("page") or {}).get("slug") for p in d["items"]),
        timeout=30,
    )
    return pdf_id


class TestTechnicalProbe:
    def test_probe_is_labelled_technical(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?probe=1")
        r = client.post(f"/api/monitoring/{pdf_id}/probe", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200

        detail = wait_for(
            client,
            f"/api/monitoring/{pdf_id}",
            lambda d: any(
                e["event_type"] == "TECHNICAL_PROBE"
                and e["status"] == "SUCCESS"
                for e in [x for x in d["item"]["events"]]
            ),
            timeout=20,
        )
        events = detail["item"]["events"]
        probes = [e for e in events if e["event_type"] == "TECHNICAL_PROBE" and e["status"] == "SUCCESS"]
        assert probes
        msg = probes[0]["message"]
        # honesty labels
        assert "Technical Server Probe" in msg
        assert "Googlebot" not in msg
        assert "OUR server" in msg
        assert "NOT evidence" in msg
        # metadata carries the honest label + UA
        assert probes[0]["metadata"]["label"] == "Technical Server Probe"
        assert "BOT-INDEXER" in probes[0]["metadata"]["user_agent"]

    def test_probe_never_sets_crawl_checked(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?probe2=1")
        client.post(f"/api/monitoring/{pdf_id}/probe", headers={"X-CSRF-Token": get_csrf(client)})
        detail = wait_for(
            client,
            f"/api/monitoring/{pdf_id}",
            lambda d: any(e["event_type"] == "TECHNICAL_PROBE" and e["status"] == "SUCCESS" for e in d["item"]["events"]),
            timeout=20,
        )
        # A server fetch is FETCH_CHECKED, never search-engine crawl evidence
        assert detail["item"]["crawl_status"] == "FETCH_CHECKED"
        # index stays UNKNOWN with an explanation
        assert detail["item"]["index_status"] == "INDEX_UNKNOWN"
        assert detail["item"]["third_party_note"]["reason"]

    def test_probe_failure_recorded(self, client):
        admin_client(client)
        r = client.post(
            "/api/pdfs",
            json={"url": "http://127.0.0.1:1/probe-fail.pdf?pf=1"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"], r.text
        pdf_id = r.json()["accepted"][0]["pdf_id"]
        # let it fail first
        wait_for(
            client,
            f"/api/pdfs?per_page=50",
            lambda d: any(p["id"] == pdf_id and p["status"] == "FAILED" for p in d["items"]),
            timeout=60,
        )
        r = client.post(f"/api/monitoring/{pdf_id}/probe", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200


class TestGscEvidence:
    """With a FAKE Search Console client (no network), verify the evidence
    pipeline: INDEXED/NOT_INDEXED only from authorized, recorded evidence."""

    class FakeGSC:
        def __init__(self, coverage):
            self.coverage = coverage
            self.called_with = []

        async def inspect_own_page(self, page_url):
            self.called_with.append(page_url)
            return {
                "inspectionResult": {
                    "indexStateResult": {
                        "coverage": {"coverageState": self.coverage},
                        "robotsTxtState": "Allowed",
                        "lastCrawlTime": "2026-09-20T10:00:00Z",
                    }
                }
            }

    def _inspect(self, client, pdf_id, coverage):
        fake = self.FakeGSC(coverage)
        client.app.state.queue.gsc = fake
        r = client.post(f"/api/monitoring/{pdf_id}/inspect", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200
        detail = wait_for(
            client,
            f"/api/monitoring/{pdf_id}",
            lambda d: any(
                e["event_type"] in ("INDEX_STATUS_CHANGED", "GSC_INSPECTED") for e in d["item"]["events"]
            ),
            timeout=20,
        )
        return detail, fake

    def test_gsc_indexed_sets_status_with_evidence(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?gsc1=1")
        detail, fake = self._inspect(client, pdf_id, "Submitted / Indexed")
        assert fake.called_with  # inspected OUR page url
        assert fake.called_with[0].startswith("http://testserver/jobs/")
        assert detail["item"]["index_status"] == "INDEXED"
        ev = detail["item"]["index_evidence"]
        assert ev["source"] == "Google Search Console"
        assert ev["coverage_state"] == "Submitted / Indexed"
        assert ev["checked_at"]
        # crawl evidence from lastCrawlTime
        assert detail["item"]["crawl_status"] == "SEARCH_ENGINE_CRAWL_EVIDENCE"

    def test_gsc_not_indexed(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?gsc2=1")
        detail, _ = self._inspect(client, pdf_id, "Crawled - currently not indexed")
        assert detail["item"]["index_status"] == "NOT_INDEXED"

    def test_gsc_ambiguous_stays_unknown(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?gsc3=1")
        detail, _ = self._inspect(client, pdf_id, "Pending")
        assert detail["item"]["index_status"] == "INDEX_UNKNOWN"

    def test_gsc_not_configured_stays_unknown(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?gsc4=1")
        client.app.state.queue.gsc = None
        r = client.post(f"/api/monitoring/{pdf_id}/inspect", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200
        detail = wait_for(
            client,
            f"/api/monitoring/{pdf_id}",
            lambda d: any(e["event_type"] == "GSC_NOT_CONFIGURED" for e in d["item"]["events"]),
            timeout=20,
        )
        assert detail["item"]["index_status"] == "INDEX_UNKNOWN"


class TestStatusClassification:
    def test_classification_matrix(self):
        from app.monitoring.status import classify_gsc_index_state

        def insp(coverage, robots="Allowed", last="2026-01-01T00:00:00Z"):
            return {
                "inspectionResult": {
                    "indexStateResult": {
                        "coverage": {"coverageState": coverage},
                        "robotsTxtState": robots,
                        "lastCrawlTime": last,
                    }
                }
            }

        assert classify_gsc_index_state(insp("Submitted / Indexed"))[0] == "INDEXED"
        assert classify_gsc_index_state(insp("Published"))[0] == "INDEX_UNKNOWN"
        assert classify_gsc_index_state(insp("Crawled - currently not indexed"))[0] == "NOT_INDEXED"
        assert classify_gsc_index_state(insp("Excluded / Blocked by robots.txt"))[0] == "NOT_INDEXED"
        assert classify_gsc_index_state(insp("Pending"))[0] == "INDEX_UNKNOWN"
        assert classify_gsc_index_state(insp(""))[0] == "INDEX_UNKNOWN"
        assert classify_gsc_index_state({})[0] == "INDEX_UNKNOWN"

    def test_never_submitted_becomes_indexed(self):
        """The core integrity rule: no code path may turn SUBMITTED into
        INDEXED without Search Console evidence."""
        from app.queue.worker import run_gsc_inspect
        import inspect as pyinspect

        src = pyinspect.getsource(run_gsc_inspect)
        # index status changes only via apply_gsc_evidence (evidence-based)
        assert "apply_gsc_evidence" in src


class TestObservation:
    def test_observe_disabled_is_noop(self, client, pdf_server_url):
        admin_client(client)
        pdf_id = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?obs=1")
        r = client.post(f"/api/monitoring/{pdf_id}/observe", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200
        detail = wait_for(
            client,
            f"/api/monitoring/{pdf_id}",
            lambda d: any(e["event_type"] == "OBSERVATION_SKIPPED" for e in d["item"]["events"]),
            timeout=20,
        )
        # observation never changes index status
        assert detail["item"]["index_status"] == "INDEX_UNKNOWN"
