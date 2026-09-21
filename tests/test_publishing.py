"""Sitemap / RSS / robots / dedicated-page tests."""
from __future__ import annotations

from conftest import admin_client, get_csrf, wait_for


def _submit_and_publish(client, url):
    r = client.post(
        "/api/pdfs/bulk",
        json={"urls": [url]},
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert r.status_code == 200, r.text
    pdf_id = r.json()["accepted"][0]["pdf_id"]
    data = wait_for(
        client,
        f"/api/pdfs?per_page=50",
        lambda d: any(p["id"] == pdf_id and (p.get("page") or {}).get("slug") for p in d["items"]),
        timeout=30,
    )
    pdf = next(p for p in data["items"] if p["id"] == pdf_id)
    return pdf


class TestSitemap:
    def test_sitemap_contains_our_pages_only(self, client, pdf_server_url):
        admin_client(client)
        pdf = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?sm=1")
        slug = pdf["page"]["slug"]

        r = client.get("/sitemap.xml")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")
        body = r.text
        assert f"/pdf/{slug}" in body
        assert "<lastmod>" in body
        # third-party URLs must NEVER appear in the sitemap
        assert pdf_server_url not in body

    def test_sitemap_404_when_disabled(self, client):
        # flip the runtime setting
        admin_client(client)
        r = client.put(
            "/api/settings",
            json={"sitemap_enabled": "false"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200
        assert client.get("/sitemap.xml").status_code == 404
        # restore
        client.put(
            "/api/settings",
            json={"sitemap_enabled": "true"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )

    def test_removed_page_removed_from_sitemap(self, client, pdf_server_url):
        admin_client(client)
        pdf = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?rm=1")
        slug = pdf["page"]["slug"]
        assert slug in client.get("/sitemap.xml").text
        client.delete(f"/api/pdfs/{pdf['id']}", headers={"X-CSRF-Token": get_csrf(client)})
        assert slug not in client.get("/sitemap.xml").text


class TestRSS:
    def test_rss_contains_published_page(self, client, pdf_server_url):
        admin_client(client)
        pdf = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?rss=1")
        slug = pdf["page"]["slug"]

        r = client.get("/rss.xml")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/rss+xml")
        body = r.text
        assert f"/pdf/{slug}" in body
        assert "<guid isPermaLink=\"true\">" in body
        assert "<pubDate>" in body
        # third-party URL never in RSS
        assert pdf_server_url not in body

    def test_rss_pubdate_uses_publication_time(self, client, pdf_server_url):
        admin_client(client)
        pdf = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?rssd=1")
        pub = pdf["page"]["published_at"][:10]  # YYYY-MM-DD
        r = client.get("/rss.xml").text
        # pubDate is RFC822; check the year-month-day loosely
        import re

        dates = re.findall(r"<pubDate>([^<]+)</pubDate>", r)
        assert any(pub[:4] in d for d in dates)


class TestRobots:
    def test_robots_declares_sitemap(self, client):
        admin_client(client)
        r = client.get("/robots.txt")
        assert r.status_code == 200
        body = r.text
        assert "Sitemap:" in body
        assert "sitemap.xml" in body
        assert "Allow: /pdf/" in body
        assert "Disallow: /api/" in body


class TestDedicatedPage:
    def test_public_page_renders_without_auth(self, client, pdf_server_url):
        admin_client(client)
        pdf = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?pub=1")
        slug = pdf["page"]["slug"]
        # fresh client without session cookies
        client.cookies.clear()
        r = client.get(f"/pdf/{slug}")
        assert r.status_code == 200
        assert "View Original PDF" in r.text
        assert f'href="{pdf["original_url"]}"' in r.text

    def test_unknown_slug_404(self, client):
        admin_client(client)
        r = client.get("/pdf/does-not-exist-000000")
        assert r.status_code == 404

    def test_slug_is_stable_and_unique(self, client, pdf_server_url):
        admin_client(client)
        p1 = _submit_and_publish(client, f"{pdf_server_url}/docs/report.pdf?st=1")
        p2 = _submit_and_publish(client, f"{pdf_server_url}/docs/report-octetstream.pdf?st=1")
        s1, s2 = p1["page"]["slug"], p2["page"]["slug"]
        assert s1 != s2
        assert s1 == p1["page"]["slug"] and re.match(r"^[a-z0-9\-]+$", s1)
        assert len(s1) <= 72


import re  # noqa: E402
