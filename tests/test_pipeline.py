"""End-to-end PDF pipeline tests against the local PDF server."""
from __future__ import annotations

import hashlib

import pytest

from conftest import ADMIN_PASSWORD, USER_EMAIL, USER_PASSWORD, admin_client, get_csrf, login, wait_for


@pytest.fixture(autouse=True)
def _admin(client):
    admin_client(client)
    yield


def submit(client, urls):
    r = client.post(
        "/api/pdfs/bulk",
        json={"urls": urls if isinstance(urls, list) else [urls]},
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def wait_pdf(client, pdf_id, timeout=30):
    return wait_for(
        client,
        f"/api/pdfs?per_page=50",
        lambda d: any(
            p["id"] == pdf_id and p["status"] not in (
                "RECEIVED", "VALIDATING", "PDF_ANALYZING", "PAGE_GENERATING"
            )
            for p in d["items"]
        ),
        timeout=timeout,
    )


class TestHappyPath:
    def test_full_pipeline_publishes_page(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/report.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)

        assert pdf["status"] == "PAGE_PUBLISHED"
        assert pdf["classification"] == "TEXT_PDF"
        assert pdf["title"] == "Renew Request Form 2026"
        assert pdf["page_count"] == 1
        assert pdf["author"] == "DECS Testing Office"
        assert pdf["content_type"] == "application/pdf"
        assert pdf["http_status"] == 200
        assert pdf["sha256"] and len(pdf["sha256"]) == 64

        # dedicated page exists and is public
        page = pdf["page"]
        assert page and page["slug"]
        slug = page["slug"]
        r = client.get(f"/pdf/{slug}")
        assert r.status_code == 200
        html = r.text
        # original link is a normal HTML anchor (no JS)
        assert f'href="{pdf_server_url}/docs/report.pdf"' in html
        assert "View Original PDF" in html
        # SEO metadata
        assert "<link rel=\"canonical\"" in html
        assert 'name="description"' in html
        assert "application/ld+json" in html
        # it does not claim to host the file
        assert "hosted by" in html.lower() or "original publisher" in html.lower()

    def test_octet_stream_content_type_still_valid(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/report-octetstream.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "PAGE_PUBLISHED"
        assert pdf["classification"] == "TEXT_PDF"

    def test_redirect_chain_followed_and_validated(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/redirect.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "PAGE_PUBLISHED"
        assert pdf["final_url"].endswith("/docs/report.pdf")

    def test_sha256_matches_file(self, client, pdf_server_url):
        import urllib.request

        raw = urllib.request.urlopen(f"{pdf_server_url}/docs/report.pdf").read()
        expected = hashlib.sha256(raw).hexdigest()
        res = submit(client, f"{pdf_server_url}/docs/report.pdf?v=2")
        # different query → different normalized URL → not a duplicate
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["sha256"] == expected


class TestErrorPaths:
    def test_http_404_is_invalid(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/missing.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "INVALID"
        assert "404" in (pdf.get("error") or "")

    def test_http_403_is_invalid(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/forbidden.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "INVALID"
        assert "403" in (pdf.get("error") or "")

    def test_html_masquerading_as_pdf_is_rejected(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/fake.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "PDF_INVALID"
        assert "signature" in (pdf.get("error") or "").lower()

    def test_retryable_5xx_then_failure(self, client):
        # connect-refused port: retryable, must eventually FAIL (not hang)
        res = submit(client, "http://127.0.0.1:1/x.pdf")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id, timeout=60)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "FAILED"
        assert pdf.get("error")

    def test_invalid_urls_rejected_at_submission(self, client):
        res = submit(
            client,
            [
                "javascript:alert(1)",
                "file:///etc/passwd",
                "ftp://x.com/a.pdf",
                "http://192.168.0.9/inner.pdf",  # private, but allowed in test env!
            ],
        )
        invalid_urls = {i["url"] for i in res["invalid"]}
        assert "javascript:alert(1)" in invalid_urls
        assert "file:///etc/passwd" in invalid_urls
        assert "ftp://x.com/a.pdf" in invalid_urls
        # in test mode private targets are allowed (dev convenience flag)
        assert len(res["accepted"]) == 1


class TestDuplicates:
    def test_duplicate_url_detected(self, client, pdf_server_url):
        url = f"{pdf_server_url}/docs/report.pdf?dupA=1"
        res1 = submit(client, url)
        pdf1 = res1["accepted"][0]["pdf_id"]
        wait_pdf(client, pdf1)
        res2 = submit(client, url)
        assert res2["accepted"] == []
        assert res2["duplicates"][0]["existing_pdf_id"] == pdf1

    def test_same_content_different_url_flagged(self, client, pdf_server_url):
        # identical file bytes under two different URLs
        res1 = submit(client, f"{pdf_server_url}/docs/report.pdf?dupB=1")
        pdf1 = res1["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf1)
        sha1 = next(p for p in data["items"] if p["id"] == pdf1)["sha256"]

        res2 = submit(client, f"{pdf_server_url}/docs/report-octetstream.pdf?dupB=1")
        pdf2 = res2["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf2)
        pdf2row = next(p for p in data["items"] if p["id"] == pdf2)
        assert pdf2row["sha256"] == sha1

        detail = client.get(f"/api/pdfs/{pdf2}").json()
        events = detail["events"]
        assert any(e["event_type"] == "SAME_PDF_CONTENT" for e in events)


class TestOwnership:
    def test_user_submits_and_watches_own_pdf(self, client, pdf_server_url):
        # ensure the regular user exists (self-contained, order-independent)
        admin_client(client)
        r = client.post(
            "/api/users",
            json={"name": "Normal User", "email": USER_EMAIL, "password": USER_PASSWORD, "role": "USER"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code in (200, 409), r.text
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        csrf = get_csrf(client)
        r = client.post(
            "/api/pdfs",
            json={"url": f"{pdf_server_url}/docs/report.pdf?own=1"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200
        pdf_id = r.json()["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "PAGE_PUBLISHED"
        # user sees it in their own list
        r = client.get("/api/pdfs")
        assert any(p["id"] == pdf_id for p in r.json()["items"])


class TestRetry:
    def test_retry_endpoint_requeues(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/missing.pdf?retry=1")  # 404 → INVALID
        pdf_id = res["accepted"][0]["pdf_id"]
        wait_pdf(client, pdf_id)
        r = client.post(f"/api/pdfs/{pdf_id}/retry", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200
        assert r.json()["status"] == "QUEUED"
        # runs again and lands INVALID again (404 is definitive)
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        assert pdf["status"] == "INVALID"

    def test_delete_removes_record_and_page(self, client, pdf_server_url):
        res = submit(client, f"{pdf_server_url}/docs/report.pdf?d=3")
        pdf_id = res["accepted"][0]["pdf_id"]
        data = wait_pdf(client, pdf_id)
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        slug = pdf["page"]["slug"]

        r = client.delete(f"/api/pdfs/{pdf_id}", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 200

        r = client.get(f"/api/pdfs?per_page=100")
        assert all(p["id"] != pdf_id for p in r.json()["items"])
        # page gone → 404 and not in sitemap
        assert client.get(f"/pdf/{slug}").status_code == 404
        sitemap = client.get("/sitemap.xml").text
        assert slug not in sitemap
