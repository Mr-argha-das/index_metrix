"""Opt-in integration test against the external sample PDF (section 42).

The sample URL is third-party and may go away; the test MUST fail gracefully
(skip) when the network is unavailable — it never breaks the suite.
"""
from __future__ import annotations

import socket

import pytest

from conftest import admin_client, get_csrf, wait_for

SAMPLE_URL = (
    "http://www.egr.msu.edu/decs/sites/default/files/webform/"
    "poster_print_request/_sid_/nainaxcvv-renew-2.pdf"
)


@pytest.mark.integration
def test_sample_pdf_url_processed_gracefully(client):
    # quick connectivity check — skip (not fail) when offline
    try:
        socket.create_connection(("www.egr.msu.edu", 80), timeout=5).close()
    except OSError:
        pytest.skip("external sample host unreachable from this environment")

    admin_client(client)
    r = client.post(
        "/api/pdfs",
        json={"url": SAMPLE_URL},
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert r.status_code == 200
    body = r.json()
    if body.get("duplicates"):
        pdf_id = body["duplicates"][0]["existing_pdf_id"]
    elif body.get("accepted"):
        pdf_id = body["accepted"][0]["pdf_id"]
    else:
        # validation rejected it — acceptable as long as the reason is clean
        assert body["invalid"]
        assert body["invalid"][0]["reason"]
        return

    data = wait_for(
        client,
        "/api/pdfs?per_page=50",
        lambda d: any(
            p["id"] == pdf_id and p["status"] not in ("RECEIVED", "VALIDATING", "PDF_ANALYZING", "PAGE_GENERATING")
            for p in d["items"]
        ),
        timeout=120,
    )
    pdf = next(p for p in data["items"] if p["id"] == pdf_id)
    # whichever the outcome, it must be a CLEAN terminal state (no crash)
    assert pdf["status"] in (
        "PAGE_PUBLISHED",
        "INVALID",
        "PDF_INVALID",
        "FAILED",
    )
