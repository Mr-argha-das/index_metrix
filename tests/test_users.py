"""User management tests."""
from __future__ import annotations

import pytest

from conftest import ADMIN_EMAIL, get_csrf, login, admin_client


@pytest.fixture(autouse=True)
def _admin(client):
    admin_client(client)
    yield


def csrf(client):
    return {"X-CSRF-Token": get_csrf(client)}


class TestUserCreation:
    def test_create_user(self, client):
        r = client.post(
            "/api/users",
            json={
                "name": "Jane Doe",
                "email": "jane@example.com",
                "password": "Jane12345",
                "role": "USER",
                "status": "ACTIVE",
            },
            headers=csrf(client),
        )
        assert r.status_code == 200, r.text
        u = r.json()["user"]
        assert u["email"] == "jane@example.com"
        assert u["role"] == "USER"
        assert "password_hash" not in u

    def test_create_admin(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Rooty", "email": "rooty@example.com", "password": "Root12345", "role": "ADMIN"},
            headers=csrf(client),
        )
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "ADMIN"

    def test_create_with_invalid_email(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Bad", "email": "not-an-email", "password": "Bad12345"},
            headers=csrf(client),
        )
        assert r.status_code == 400

    def test_create_with_weak_password(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Weak", "email": "weak@example.com", "password": "short"},
            headers=csrf(client),
        )
        assert r.status_code == 400
        assert "8 characters" in r.json()["detail"]

    def test_create_with_weak_password_no_digit(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Weak", "email": "weak2@example.com", "password": "lettersonly"},
            headers=csrf(client),
        )
        assert r.status_code == 400

    def test_create_duplicate_email(self, client):
        client.post(
            "/api/users",
            json={"name": "Dup", "email": "dup@example.com", "password": "Dup12345"},
            headers=csrf(client),
        )
        r = client.post(
            "/api/users",
            json={"name": "Dup2", "email": "dup@example.com", "password": "Dup12345"},
            headers=csrf(client),
        )
        assert r.status_code == 409

    def test_create_with_invalid_role(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Bad", "email": "badrole@example.com", "password": "Bad12345", "role": "SUPER"},
            headers=csrf(client),
        )
        assert r.status_code == 400


class TestUserLifecycle:
    def test_disable_and_enable(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Toggle", "email": "toggle@example.com", "password": "Toggle123"},
            headers=csrf(client),
        )
        uid = r.json()["user"]["id"]

        r = client.put(f"/api/users/{uid}", json={"name": "Toggle", "email": "toggle@example.com", "role": "USER", "status": "DISABLED"}, headers=csrf(client))
        assert r.status_code == 200
        assert r.json()["user"]["status"] == "DISABLED"

        # disabled user cannot log in (separate client: keeps the shared
        # client's admin session intact)
        from conftest import fresh_client

        c2 = fresh_client()
        login(c2, "toggle@example.com", "Toggle123", new=True)
        assert c2.get("/api/auth/me").status_code == 401

        r = client.put(f"/api/users/{uid}", json={"name": "Toggle", "email": "toggle@example.com", "role": "USER", "status": "ACTIVE"}, headers=csrf(client))
        assert r.json()["user"]["status"] == "ACTIVE"

    def test_password_reset_invalidates_sessions(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Reset", "email": "reset@example.com", "password": "OldPass123"},
            headers=csrf(client),
        )
        uid = r.json()["user"]["id"]

        # log in as that user
        login(client, "reset@example.com", "OldPass123", new=True)
        assert client.get("/api/auth/me").status_code == 200

        # admin resets password
        admin_client(client)
        r = client.post(
            f"/api/users/{uid}/reset-password",
            json={"password": "NewPass123"},
            headers=csrf(client),
        )
        assert r.status_code == 200, r.text

        # old session invalidated
        client.cookies.clear()  # fresh cookies: session cookie gone
        login(client, "reset@example.com", "OldPass123", new=True)
        assert client.get("/api/auth/me").status_code == 401

        # new password works
        login(client, "reset@example.com", "NewPass123", new=True)
        assert client.get("/api/auth/me").status_code == 200

    def test_cannot_disable_self(self, client):
        me = client.get("/api/auth/me").json()["user"]
        r = client.put(
            f"/api/users/{me['id']}",
            json={"name": me["name"], "email": me["email"], "role": "ADMIN", "status": "DISABLED"},
            headers=csrf(client),
        )
        assert r.status_code == 400

    def test_cannot_delete_self(self, client):
        me = client.get("/api/auth/me").json()["user"]
        r = client.delete(f"/api/users/{me['id']}", headers=csrf(client))
        assert r.status_code == 400

    def test_delete_user(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Doomed", "email": "doomed@example.com", "password": "Doom12345"},
            headers=csrf(client),
        )
        uid = r.json()["user"]["id"]
        r = client.delete(f"/api/users/{uid}", headers=csrf(client))
        assert r.status_code == 200
        r = client.get("/api/users")
        assert all(u["id"] != uid for u in r.json()["users"])

    def test_cannot_demote_last_admin(self, client):
        me = client.get("/api/auth/me").json()["user"]
        # demote any OTHER admins so we are the only active admin
        for u in client.get("/api/users").json()["users"]:
            if u["id"] != me["id"] and u["role"] == "ADMIN" and u["status"] == "ACTIVE":
                r = client.put(
                    f"/api/users/{u['id']}",
                    json={"name": u["name"], "email": u["email"], "role": "USER", "status": "ACTIVE"},
                    headers=csrf(client),
                )
                assert r.status_code == 200, r.text
        # we are the only active admin
        r = client.put(
            f"/api/users/{me['id']}",
            json={"name": me["name"], "email": me["email"], "role": "USER", "status": "ACTIVE"},
            headers=csrf(client),
        )
        assert r.status_code == 400
        assert "last active administrator" in r.json()["detail"]

    def test_edit_user_name_and_role(self, client):
        r = client.post(
            "/api/users",
            json={"name": "Ed", "email": "ed@example.com", "password": "Ed123456"},
            headers=csrf(client),
        )
        uid = r.json()["user"]["id"]
        r = client.put(
            f"/api/users/{uid}",
            json={"name": "Edwin", "email": "ed@example.com", "role": "USER", "status": "ACTIVE"},
            headers=csrf(client),
        )
        assert r.status_code == 200
        assert r.json()["user"]["name"] == "Edwin"
