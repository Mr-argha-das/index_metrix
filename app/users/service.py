"""User management service (admin operations)."""
from __future__ import annotations

import logging

from ..auth.routes import public_user
from ..auth.security import hash_password, needs_rehash, verify_password
from ..auth.service import AuthError, validate_email
from ..database.repositories import Repos
from ..utils import utcnow_iso, password_strength_error

log = logging.getLogger("bot_indexer.users")

ROLES = ("ADMIN", "USER")
STATUSES = ("ACTIVE", "DISABLED")


class UserService:
    def __init__(self, repos: Repos):
        self.repos = repos

    # -- helpers -------------------------------------------------------------

    async def _get_or_404(self, user_id: int) -> dict:
        user = await self.repos.users.get(user_id)
        if not user:
            raise AuthError("User not found.", 404)
        return user

    async def _ensure_email_unique(self, email: str, exclude_id: int | None = None) -> None:
        rows = await self.repos.users.find(lambda r: r.get("email") == email)
        for row in rows:
            if row["id"] != exclude_id:
                raise AuthError(f"A user with email '{email}' already exists.", 409)

    # -- operations ------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        email: str,
        password: str,
        role: str = "USER",
        status: str = "ACTIVE",
        actor: dict,
    ) -> dict:
        email = validate_email(email)
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise AuthError("A user name (1-120 characters) is required.", 400)
        strength = password_strength_error(password or "")
        if strength:
            raise AuthError(strength, 400)
        role = (role or "USER").upper()
        if role not in ROLES:
            raise AuthError(f"Role must be one of {', '.join(ROLES)}.", 400)
        status = (status or "ACTIVE").upper()
        if status not in STATUSES:
            raise AuthError(f"Status must be one of {', '.join(STATUSES)}.", 400)
        await self._ensure_email_unique(email)

        user = await self.repos.users.insert(
            name=name,
            email=email,
            password_hash=hash_password(password),
            role=role,
            status=status,
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
            last_login=None,
            created_by=actor.get("id"),
        )
        await self.repos.events.add(
            "USER_CREATED",
            f"User '{email}' (role {role}) created by {actor.get('email')}",
            user_id=user["id"],
            status="SUCCESS",
        )
        log.info("user_id=%s event=USER_CREATED role=%s", user["id"], role)
        return public_user(user)

    async def update(
        self, user_id: int, *, actor: dict, **fields
    ) -> dict:
        user = await self._get_or_404(user_id)
        if "name" in fields and fields["name"] is not None:
            name = str(fields["name"]).strip()
            if not name or len(name) > 120:
                raise AuthError("A user name (1-120 characters) is required.", 400)
            fields["name"] = name
        if "email" in fields and fields["email"] is not None:
            fields["email"] = validate_email(fields["email"])
            await self._ensure_email_unique(fields["email"], exclude_id=user_id)
        if "role" in fields and fields["role"] is not None:
            role = str(fields["role"]).upper()
            if role not in ROLES:
                raise AuthError(f"Role must be one of {', '.join(ROLES)}.", 400)
            # never demote the last active admin
            if user["role"] == "ADMIN" and role != "ADMIN":
                admins = await self.repos.users.find(
                    lambda r: r.get("role") == "ADMIN" and r.get("status") == "ACTIVE"
                )
                if len(admins) <= 1:
                    raise AuthError(
                        "Cannot demote the last active administrator.", 400
                    )
            fields["role"] = role
        if "status" in fields and fields["status"] is not None:
            status = str(fields["status"]).upper()
            if status not in STATUSES:
                raise AuthError(f"Status must be one of {', '.join(STATUSES)}.", 400)
            if user["id"] == actor.get("id") and status == "DISABLED":
                raise AuthError("You cannot disable your own account.", 400)
            if (
                user["role"] == "ADMIN"
                and status == "DISABLED"
                and user["status"] == "ACTIVE"
            ):
                admins = await self.repos.users.find(
                    lambda r: r.get("role") == "ADMIN" and r.get("status") == "ACTIVE"
                )
                if len(admins) <= 1:
                    raise AuthError(
                        "Cannot disable the last active administrator.", 400
                    )
            fields["status"] = status
        if not fields:
            return public_user(user)

        updated = await self.repos.users.update(user_id, **fields)
        await self.repos.events.add(
            "USER_UPDATED",
            f"User '{user['email']}' updated: {', '.join(sorted(fields))}",
            user_id=user_id,
            status="SUCCESS",
        )
        # refresh the password hash algorithm if the stored one is outdated
        return public_user(updated)

    async def set_status(self, user_id: int, status: str, actor: dict) -> dict:
        return await self.update(user_id, actor=actor, status=status)

    async def delete(self, user_id: int, actor: dict) -> None:
        user = await self._get_or_404(user_id)
        if user["id"] == actor.get("id"):
            raise AuthError("You cannot delete your own account.", 400)
        if user["role"] == "ADMIN":
            admins = await self.repos.users.find(
                lambda r: r.get("role") == "ADMIN" and r.get("status") == "ACTIVE"
            )
            if len(admins) <= 1:
                raise AuthError("Cannot delete the last administrator.", 400)
        ok = await self.repos.users.delete(user_id)
        if not ok:
            raise AuthError("User not found.", 404)
        # cascade: drop sessions; keep pdfs/jobs/events for audit (set user_id context)
        sessions = await self.repos.sessions.find(lambda r: r.get("user_id") == user_id)
        for s in sessions:
            await self.repos.sessions.delete(s["id"])
        await self.repos.events.add(
            "USER_DELETED",
            f"User '{user['email']}' deleted by {actor.get('email')}",
            user_id=user_id,
            status="SUCCESS",
        )
        log.info("user_id=%s event=USER_DELETED", user_id)

    async def reset_password(self, user_id: int, new_password: str, actor: dict) -> dict:
        user = await self._get_or_404(user_id)
        strength = password_strength_error(new_password or "")
        if strength:
            raise AuthError(strength, 400)
        await self.repos.users.update(user_id, password_hash=hash_password(new_password))
        # invalidate all sessions for that user
        sessions = await self.repos.sessions.find(lambda r: r.get("user_id") == user_id)
        for s in sessions:
            await self.repos.sessions.delete(s["id"])
        await self.repos.events.add(
            "PASSWORD_RESET",
            f"Password for '{user['email']}' reset by {actor.get('email')}; sessions invalidated",
            user_id=user_id,
            status="SUCCESS",
        )
        return public_user(await self._get_or_404(user_id))

    async def list(self) -> list[dict]:
        rows = await self.repos.users.all(order="id")
        return [public_user(r) for r in rows]
