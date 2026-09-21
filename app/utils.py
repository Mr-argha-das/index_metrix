"""Shared utility helpers."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def slugify(text: str, max_len: int = 48) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


def short_hash(value: str, length: int = 6) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:length]


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.split("@")[-1].split(":")[0]
        return host.lower()
    except Exception:
        return ""


def truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def jsonable(obj: Any) -> Any:
    """Make data JSON-safe (float NaN -> None, integral floats -> int,
    bytes -> str, datetime -> iso)."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        return int(obj) if obj.is_integer() else obj
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return obj


def secure_cookie_params(
    scheme: str | None,
    x_forwarded_proto: str | None,
    host: str | None,
    settings,
) -> tuple[bool, str]:
    """Return ``(secure, samesite)`` for cookies set on this request.

    Cross-site embeds (e.g. an HTTPS live-preview iframe) need
    ``SameSite=None; Secure`` cookies or the browser will not send them on
    fetches, which breaks both the CSRF double-submit pattern and sessions.
    Plain-HTTP local development keeps ``SameSite=Lax`` (Secure cookies are
    not sent over http).

    ``settings.cookie_samesite`` (env ``COOKIE_SAMESITE``: lax|none) forces a
    mode; ``settings.secure_client`` (env ``SECURE_CLIENT``) force-secure.
    """
    override = (getattr(settings, "cookie_samesite", "") or "").lower()
    if override in ("lax", "none"):
        return override == "none", override
    secure = (
        (scheme or "").lower() == "https"
        or (x_forwarded_proto or "").split(",")[0].strip().lower() == "https"
        or (host or "").lower().endswith(".e2b.app")
        or bool(getattr(settings, "secure_client", False))
        or bool(getattr(settings, "secure_cookies", False))
    )
    return secure, ("none" if secure else "lax")


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * min(len(value) - keep, 8)


def password_strength_error(password: str) -> str | None:
    """Return an error message if the password is too weak, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 128:
        return "Password must be at most 128 characters long."
    if not re.search(r"[A-Za-z]", password):
        return "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one number."
    return None
