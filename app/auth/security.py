"""Password hashing and session token helpers.

* Passwords: Argon2id via ``argon2-cffi`` (memory-hard, salted).
* Sessions: high-entropy random tokens; only the SHA-256 hash of the token is
  persisted in ``sessions.feather``. The cookie carries the raw token, and is
  flagged ``HttpOnly`` + ``SameSite=Lax`` (+ ``Secure`` in production).
"""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError

_ph = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16
)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, Argon2Error, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


def generate_token() -> str:
    """URL-safe 256-bit session token."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_tokens(a: str, b: str) -> bool:
    return secrets.compare_digest(a or "", b or "")
