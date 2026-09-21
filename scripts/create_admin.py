#!/usr/bin/env python
"""Create an administrator (or any user) account from the command line.

    python scripts/create_admin.py --email admin@example.com --name "Ada Admin"
    python scripts/create_admin.py --email ops@example.com --name "Ops User" --role USER

The password is entered securely at the prompt (never shown, never logged).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.feather_store import Database  # noqa: E402
from app.database.repositories import Repos  # noqa: E402
from app.auth.security import hash_password  # noqa: E402
from app.utils import utcnow_iso, password_strength_error  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a user (default role: ADMIN)")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", default="ADMIN", choices=["ADMIN", "USER"])
    parser.add_argument("--password", default=None, help="pass directly (otherwise prompted)")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.data_dir)
    db.init()
    repos = Repos(db)

    email = args.email.strip().lower()
    existing = repos.users.find(lambda r: r.get("email") == email)
    if existing:
        print(f"Error: a user with email {email} already exists (id {existing[0]['id']}).")
        return 1

    password = args.password or getpass.getpass("Choose a password (min 8 chars, letters + number): ")
    strength = password_strength_error(password)
    if strength:
        print(f"Error: {strength}")
        return 1

    user = repos.users.insert(
        name=args.name,
        email=email,
        password_hash=hash_password(password),
        role=args.role,
        status="ACTIVE",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
        last_login=None,
        created_by=None,
    )
    repos.events.add(
        "USER_CREATED",
        f"User '{email}' (role {args.role}) created via scripts/create_admin.py",
        user_id=user["id"],
        status="SUCCESS",
    )
    print(f"Created {args.role.lower()} user id={user['id']} email={email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
