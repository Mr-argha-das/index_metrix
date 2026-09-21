"""System endpoints: health, status, logs, settings, integrations."""
from __future__ import annotations

import pytest

from conftest import admin_client, get_csrf


class TestHealth:
    def test_health_shape(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert body["queue"] == "ok"
        assert body["version"]
        assert body["timestamp"]
        # no secrets leak
        text = str(body)
        assert "secret" not in text.lower() or "session_secret" not in text


class TestSystemStatus:
    def test_status_requires_admin(self, client):
        admin_client(client)
        r = client.get("/api/system/status")
        assert r.status_code == 200
        body = r.json()
        assert body["environment"] == "test"
        assert "tables" in body
        assert body["integrations"]["google_search_console"]["configured"] is False
        assert body["integrations"]["bing_webmaster"]["configured"] is False


class TestLogs:
    def test_logs_require_admin(self, client):
        admin_client(client)
        r = client.get("/api/logs")
        assert r.status_code == 200
        assert "lines" in r.json()
        assert "events" in r.json()

    def test_logs_have_structure(self, client):
        admin_client(client)
        lines = client.get("/api/logs").json()["lines"]
        assert lines
        line = lines[0]
        for key in ("ts", "level", "logger", "message"):
            assert key in line


class TestSettings:
    def test_get_settings(self, client):
        admin_client(client)
        r = client.get("/api/settings")
        assert r.status_code == 200
        s = r.json()["settings"]
        for key in (
            "app_name",
            "public_base_url",
            "session_lifetime_hours",
            "max_pdf_size_mb",
            "http_timeout",
            "max_redirects",
            "indexer_concurrency",
            "max_retries",
            "polling_interval_ms",
            "rss_enabled",
            "sitemap_enabled",
            "google_search_console_enabled",
            "bing_webmaster_enabled",
            "observation_enabled",
        ):
            assert key in s

    def test_update_and_roundtrip(self, client):
        admin_client(client)
        r = client.put(
            "/api/settings",
            json={"indexer_concurrency": "4", "polling_interval_ms": "7000"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200
        assert r.json()["saved"]["indexer_concurrency"] == 4
        s = client.get("/api/settings").json()["settings"]
        assert s["indexer_concurrency"] == 4
        assert s["polling_interval_ms"] == 7000
        # restore
        client.put(
            "/api/settings",
            json={"indexer_concurrency": "2", "polling_interval_ms": "5000"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )

    def test_update_rejects_bad_values(self, client):
        admin_client(client)
        r = client.put(
            "/api/settings",
            json={
                "indexer_concurrency": "99",  # above clamp
                "not_a_key": "x",
                "max_pdf_size_mb": "notanumber",
            },
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200
        errors = r.json()["errors"]
        assert "indexer_concurrency" in errors
        assert "not_a_key" in errors
        assert "max_pdf_size_mb" in errors

    def test_settings_require_admin(self, client):
        from conftest import USER_EMAIL, USER_PASSWORD, login

        # ensure user exists
        admin_client(client)
        client.post(
            "/api/users",
            json={"name": "U", "email": USER_EMAIL, "password": USER_PASSWORD, "role": "USER"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        assert client.get("/api/settings").status_code == 403
        assert (
            client.put(
                "/api/settings",
                json={"app_name": "Hax"},
                headers={"X-CSRF-Token": get_csrf(client)},
            ).status_code
            == 403
        )


class TestIntegrations:
    def test_integrations_page_requires_admin(self, client):
        admin_client(client)
        assert client.get("/integrations").status_code == 200

    def test_integrations_status_not_configured(self, client):
        admin_client(client)
        r = client.get("/api/integrations")
        assert r.status_code == 200
        body = r.json()
        assert body["google"]["configured"] is False
        assert body["bing"]["configured"] is False
        assert body["google"]["status"] == "NOT CONFIGURED"
        assert body["bing"]["status"] == "NOT CONFIGURED"

    def test_google_test_not_configured(self, client):
        admin_client(client)
        r = client.post(
            "/api/integrations/google/test", headers={"X-CSRF-Token": get_csrf(client)}
        )
        assert r.status_code == 400
        assert "NOT CONFIGURED" in r.json()["detail"]

    def test_bing_test_not_configured(self, client):
        admin_client(client)
        r = client.post("/api/integrations/bing/test", headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 400
        assert "NOT CONFIGURED" in r.json()["detail"]

    def test_sitemap_submission_not_configured(self, client):
        admin_client(client)
        r = client.post(
            "/api/integrations/google/submit-sitemap", headers={"X-CSRF-Token": get_csrf(client)}
        )
        assert r.status_code == 400
        r = client.post(
            "/api/integrations/bing/submit-sitemap", headers={"X-CSRF-Token": get_csrf(client)}
        )
        assert r.status_code == 400


class TestCookieFlags:
    """Session/CSRF cookies must be SameSite=None; Secure only for secure
    clients (needed for cross-site iframes like the HTTPS live preview),
    and Lax for plain-HTTP local use."""

    def test_http_local_stays_lax(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        s = Settings()
        assert secure_cookie_params("http", None, "localhost:8000", s) == (False, "lax")

    def test_https_scheme_is_secure(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        s = Settings()
        assert secure_cookie_params("https", None, "example.com", s) == (True, "none")

    def test_forwarded_proto_https_is_secure(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        s = Settings()
        # proxy fronts the app over http internally
        assert secure_cookie_params("http", "https", "127.0.0.1:8000", s) == (True, "none")

    def test_preview_host_is_secure(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        s = Settings()
        assert secure_cookie_params("http", None, "8000-abc123.e2b.app", s) == (True, "none")

    def test_secure_client_flag(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        s = Settings(secure_client=True)
        assert secure_cookie_params("http", None, "localhost:8000", s) == (True, "none")

    def test_explicit_override_wins(self):
        from app.config import Settings
        from app.utils import secure_cookie_params

        assert secure_cookie_params(
            "https", None, "example.com", Settings(cookie_samesite="lax")
        ) == (False, "lax")
        assert secure_cookie_params(
            "http", None, "localhost", Settings(cookie_samesite="none")
        ) == (True, "none")


class TestPageQuality:
    def test_pdf_page_no_fake_authors(self, client, pdf_server_url):
        """The JSON-LD on dedicated pages must not invent authors."""
        from conftest import get_csrf, wait_for

        admin_client(client)
        # the dev PDF has an author ("DECS Testing Office"); page must show it
        r = client.post(
            "/api/pdfs",
            json={"url": f"{pdf_server_url}/docs/report.pdf"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        # may be a duplicate from other tests; find it either way
        if r.json().get("duplicates"):
            pdf_id = r.json()["duplicates"][0]["existing_pdf_id"]
        else:
            pdf_id = r.json()["accepted"][0]["pdf_id"]
        data = wait_for(
            client,
            "/api/pdfs?per_page=50",
            lambda d: any((p.get("page") or {}).get("slug") for p in d["items"] if p["id"] == pdf_id),
            timeout=30,
        )
        pdf = next(p for p in data["items"] if p["id"] == pdf_id)
        html = client.get(f"/pdf/{pdf['page']['slug']}").text
        import json as _json
        import re

        m = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        )
        assert m, "JSON-LD missing"
        ld = _json.loads(m.group(1))
        assert ld["@type"] == "WebPage"
        about = ld.get("about", {})
        assert about.get("contentUrl")
        if about.get("author"):
            assert about["author"]["name"]  # only present when PDF has an author

    def test_upload_txt(self, client, pdf_server_url):
        admin_client(client)
        content = f"{pdf_server_url}/docs/report.pdf?up=1\n{pdf_server_url}/docs/report.pdf?up=2\n"
        r = client.post(
            "/api/pdfs/file",
            files={"file": ("urls.txt", content.encode(), "text/plain")},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["accepted"]) == 2

    def test_upload_rejects_non_txt(self, client):
        admin_client(client)
        r = client.post(
            "/api/pdfs/file",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 400

    def test_upload_empty(self, client):
        admin_client(client)
        r = client.post(
            "/api/pdfs/file",
            files={"file": ("empty.txt", b"\n\n", "text/plain")},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 422
