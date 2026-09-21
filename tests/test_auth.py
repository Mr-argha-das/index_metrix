"""Authentication & authorization tests."""
from __future__ import annotations

import pytest

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, USER_EMAIL, USER_PASSWORD, admin_client, get_csrf, login


class TestLogin:
    def test_login_page_renders(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "BOT INDEXER" in r.text or "Sign in" in r.text
        # csrf cookie is set
        assert "csrf" in client.cookies

    def test_login_success(self, client):
        r = login(client, ADMIN_EMAIL, ADMIN_PASSWORD, new=True)
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["email"] == ADMIN_EMAIL
        assert body["user"]["role"] == "ADMIN"
        assert "password_hash" not in body["user"]
        # session cookie is set
        assert "session" in client.cookies

    def test_login_wrong_password(self, client):
        r = login(client, ADMIN_EMAIL, "WrongPass1", new=True)
        assert r.status_code == 401

    def test_login_unknown_email(self, client):
        r = login(client, "nobody@example.com", "WrongPass1", new=True)
        assert r.status_code == 401

    def test_me_requires_auth(self, client):
        client.cookies.clear()
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_returns_user(self, client):
        admin_client(client)
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["user"]["email"] == ADMIN_EMAIL

    def test_logout_invalidates_session(self, client):
        admin_client(client)
        csrf = get_csrf(client)
        r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_admin_bootstrap_only_once(self, client):
        # second admin must not exist after first startup
        admin_client(client)
        r = client.get("/api/users")
        emails = [u["email"] for u in r.json()["users"]]
        assert emails.count(ADMIN_EMAIL) == 1

    def test_login_rate_limiting(self, client):
        """Brute-force protection kicks in after the configured number of
        attempts (verified by temporarily tightening the live setting)."""
        import app.auth.routes as auth_routes

        client.app.state.settings = client.app.state.settings.model_copy(
            update={"login_rate_limit": 2, "login_rate_window_seconds": 300}
        )
        try:
            # clear any prior attempts for the test client IP
            auth_routes._login_attempts.clear()
            csrf = get_csrf(client)
            codes = []
            for _ in range(6):
                r = client.post(
                    "/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": "WrongPass1"},
                    headers={"X-CSRF-Token": csrf},
                )
                codes.append(r.status_code)
        finally:
            # restore the permissive test settings
            client.app.state.settings = client.app.state.settings.model_copy(
                update={"login_rate_limit": 100000}
            )
            auth_routes._login_attempts.clear()
        assert codes[:2] == [401, 401]
        assert 429 in codes[2:]


class TestAuthorization:
    @pytest.fixture(autouse=True)
    def _setup(self, client):
        admin_client(client)
        # ensure the normal user exists
        r = client.post(
            "/api/users",
            json={
                "name": "Normal User",
                "email": USER_EMAIL,
                "password": USER_PASSWORD,
                "role": "USER",
            },
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code in (200, 409)
        yield

    def test_user_cannot_list_users(self, client):
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        assert client.get("/api/users").status_code == 403

    def test_user_cannot_create_user(self, client):
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        r = client.post(
            "/api/users",
            json={"name": "X", "email": "x@e.com", "password": "X12345678", "role": "USER"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 403

    def test_user_cannot_access_admin_pages(self, client):
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        assert client.get("/users").status_code == 403
        assert client.get("/settings").status_code == 403
        assert client.get("/integrations").status_code == 403
        assert client.get("/logs").status_code == 403

    def test_user_can_access_own_pages(self, client):
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        assert client.get("/dashboard").status_code == 200
        assert client.get("/submit").status_code == 200
        assert client.get("/urls").status_code == 200
        assert client.get("/queue").status_code == 200
        assert client.get("/monitoring").status_code == 200

    def test_user_cannot_view_others_pdfs(self, client):
        # admin submits a pdf
        admin_client(client)
        r = client.post(
            "/api/pdfs",
            json={"url": "http://127.0.0.1:1/nope.pdf"},  # will fail fast (refused)
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert r.status_code == 200
        pdf_id = r.json()["accepted"][0]["pdf_id"]
        # switch to user
        login(client, USER_EMAIL, USER_PASSWORD, new=True)
        assert client.get(f"/api/pdfs/{pdf_id}").status_code == 403
        r = client.get("/api/pdfs")
        ids = [p["id"] for p in r.json()["items"]]
        assert pdf_id not in ids


class TestBearerFallback:
    """Cookie-less embedded contexts (browsers that block third-party
    cookies): login returns a token, API calls use Authorization: Bearer,
    and page loads use a one-time handoff token (?st=)."""

    def _login_no_cookies(self, c):
        """Log in, then drop all cookies so the client simulates a
        third-party-cookie-blocked context."""
        r = c.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers={"X-CSRF-Token": get_csrf(c)},
        )
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        c.cookies.clear()
        return token

    def test_login_works_without_csrf_cookie(self, client):
        """When the context sends no csrf cookie at all, login must not
        require the double-submit (the pattern cannot apply)."""
        client.cookies.clear()
        r = client.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        assert r.json()["token"]
        client.cookies.clear()

    def test_bearer_api_access(self, client):
        token = self._login_no_cookies(client)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["user"]["email"] == ADMIN_EMAIL

    def test_bearer_mutations_skip_csrf(self, client):
        token = self._login_no_cookies(client)
        r = client.post(
            "/api/pdfs",
            json={"url": "http://127.0.0.1:1/bearer-test.pdf?bt=1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

    def test_handoff_single_use_page_load(self, client):
        token = self._login_no_cookies(client)
        h = client.post("/api/auth/handoff", headers={"Authorization": f"Bearer {token}"})
        assert h.status_code == 200
        st = h.json()["st"]

        # first use: page renders
        r1 = client.get(f"/dashboard?st={st}")
        assert r1.status_code == 200
        assert "/dashboard" in str(r1.request.url)

        # second use of the same token: rejected → bounced to login
        client.cookies.clear()
        r2 = client.get(f"/dashboard?st={st}")
        assert "/login" in str(r2.request.url)

    def test_stale_bearer_rejected(self, client):
        token = self._login_no_cookies(client)
        # destroy the session via the cookie-less logout
        r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_invalid_bearer_does_not_fall_through_to_cookie(self, client):
        admin_client(client)
        client.cookies.clear()
        # cookie is gone AND bearer is bogus → unauthenticated
        r = client.get("/api/auth/me", headers={"Authorization": "Bearer bogus-token"})
        assert r.status_code == 401


class TestCSRF:
    def test_mutating_without_csrf_rejected(self, client):
        admin_client(client)
        r = client.post(
            "/api/pdfs", json={"url": "http://example.com/a.pdf"}
        )
        assert r.status_code == 403

    def test_mutating_with_bad_csrf_rejected(self, client):
        admin_client(client)
        r = client.post(
            "/api/pdfs",
            json={"url": "http://example.com/a.pdf"},
            headers={"X-CSRF-Token": "wrong-token"},
        )
        assert r.status_code == 403

    def test_get_requests_need_no_csrf(self, client):
        admin_client(client)
        assert client.get("/api/dashboard/stats").status_code == 200
