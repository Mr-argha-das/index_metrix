"""Authentication service: sessions, login/logout, CSRF, user bootstrap."""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import timedelta

from ..config import Settings
from ..database.repositories import Repos
from ..utils import parse_iso, utcnow, utcnow_iso
from .security import generate_token, hash_password, token_hash, verify_password

log = logging.getLogger("bot_indexer.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not email or not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("A valid email address is required.", 400)
    return email


async def bootstrap_admin_if_needed(repos: Repos, settings: Settings) -> dict | None:
    """Create the initial admin on first startup (only when NO users exist).

    The password is taken from the ADMIN_PASSWORD environment variable. It is
    never logged. If the variable is missing/placeholder, no admin is created
    and the operator must run ``python scripts/create_admin.py``.
    """
    try:
        users = await repos.users.all()
    except Exception:  # noqa: BLE001
        users = []
    if users:
        return None

    email = (settings.admin_email or "").strip().lower()
    password = settings.admin_password or ""
    if not email:
        return None
    if not password or password == "CHANGE_ME" or len(password) < 8:
        log.warning(
            "No users exist and ADMIN_PASSWORD is not set to a usable value. "
            "Run `python scripts/create_admin.py --email %s` to create the "
            "initial admin.",
            email or "your-admin-email",
        )
        return None

    if not EMAIL_RE.match(email):
        log.error("ADMIN_EMAIL is not a valid email; skipping admin bootstrap")
        return None

    user = await repos.users.insert(
        name="Administrator",
        email=email,
        password_hash=hash_password(password),
        role="ADMIN",
        status="ACTIVE",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
        last_login=None,
        created_by=None,
    )
    await repos.events.add(
        "USER_CREATED",
        f"Initial admin user '{email}' created from environment on first startup",
        user_id=user["id"],
        status="SUCCESS",
    )
    log.info("Bootstrapped initial admin user id=%s", user["id"])
    return user


class AuthService:
    def __init__(self, repos: Repos, settings: Settings):
        self.repos = repos
        self.settings = settings
        # One-time page grants, bound to the original (revocable) login session.
        self._handoffs: dict[str, tuple[int, float, str]] = {}

    # -- sessions ------------------------------------------------------------

    def _lifetime(self) -> timedelta:
        return timedelta(hours=self.settings.session_lifetime_hours)

    # -- handoff tokens (cookie-less embedded contexts) -----------------------

    def _prune_handoffs(self) -> None:
        now = time.time()
        for k in [k for k, (_, exp, _) in self._handoffs.items() if exp < now]:
            self._handoffs.pop(k, None)

    def create_handoff(self, user_id: int, session_token: str) -> str:
        """Mint a one-time, 60-second token that authenticates a single page
        request via ``?st=``. Used by cookie-less embeds (browsers that block
        third-party cookies) to enter server-rendered pages."""
        self._prune_handoffs()
        token = secrets.token_urlsafe(24)
        self._handoffs[token_hash(token)] = (user_id, time.time() + 60.0, session_token)
        return token

    async def consume_handoff(self, token: str | None) -> tuple[int, str] | None:
        if not token or len(token) > 200:
            return None
        key = token_hash(token)
        rec = self._handoffs.pop(key, None)
        if not rec:
            return None
        user_id, expires, session_token = rec
        if time.time() > expires:
            return None
        session = await self.get_session(session_token)
        if not session or session["user_id"] != user_id:
            return None
        return user_id, session_token

    async def create_session(self, user_id: int) -> tuple[str, str]:
        """Return (token, expires_at_iso)."""
        token = generate_token()
        expires = utcnow() + self._lifetime()
        await self.repos.sessions.insert(
            token_hash=token_hash(token),
            user_id=user_id,
            created_at=utcnow_iso(),
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            last_seen_at=utcnow_iso(),
        )
        return token, expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def get_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = await self.repos.sessions.find(lambda r: r.get("token_hash") == token_hash(token))
        if not row:
            return None
        session = row[0]
        expires = parse_iso(session.get("expires_at"))
        if not expires or expires < utcnow():
            await self.repos.sessions.delete(session["id"])
            return None
        return session

    async def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        rows = await self.repos.sessions.find(lambda r: r.get("token_hash") == token_hash(token))
        for row in rows:
            await self.repos.sessions.delete(row["id"])

    async def touch_session(self, token: str) -> None:
        rows = await self.repos.sessions.find(lambda r: r.get("token_hash") == token_hash(token))
        if rows:
            await self.repos.sessions.update(rows[0]["id"], last_seen_at=utcnow_iso())

    async def purge_expired(self) -> int:
        now = utcnow()
        n = 0

        async def _do():
            nonlocal n
            rows = await self.repos.sessions.all()
            for row in rows:
                exp = parse_iso(row.get("expires_at"))
                if not exp or exp < now:
                    await self.repos.sessions.delete(row["id"])
                    n += 1

        await _do()
        return n

    # -- authentication --------------------------------------------------------

    async def authenticate(self, email: str, password: str) -> dict:
        email = validate_email(email)
        users = await self.repos.users.find(lambda r: r.get("email") == email)
        if not users:
            # constant-ish timing: still run a hash check to reduce user enumeration
            verify_password("$argon2id$v=19$m=65536,t=3,p=2$c2FsdHNhbHQ$invalid", password)
            raise AuthError("Invalid email or password.", 401)
        user = users[0]
        if user.get("status") != "ACTIVE":
            raise AuthError("This account has been disabled.", 403)
        if not verify_password(user.get("password_hash") or "", password):
            raise AuthError("Invalid email or password.", 401)
        await self.repos.users.update(user["id"], last_login=utcnow_iso())
        return user
