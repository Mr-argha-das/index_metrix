"""Repository layer.

All read-modify-write cycles happen under the database lock and end in an
atomic Feather write, so concurrent queue workers and request handlers can
never corrupt the files.

Every repository method is async (it acquires the lock) even though the
underlying IO is synchronous and small — Feather files here stay well under
a few MB, so synchronous pandas work is acceptable and keeps the code simple.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from ..config import get_settings
from ..utils import json_loads, jsonable, utcnow_iso
from .feather_store import Database, DatabaseError

log = logging.getLogger("bot_indexer.db")


class Repository:
    def __init__(self, db: Database, table_name: str):
        self.db = db
        self.store = db.table(table_name)

    # -- low-level -----------------------------------------------------------

    async def _with_lock(self, fn: Callable[[pd.DataFrame], Any]) -> Any:
        async with self.db.lock:
            return fn(self.store.df.copy())

    # -- CRUD ------------------------------------------------------------------

    async def all(self, limit: int | None = None, order: str = "id") -> list[dict]:
        async with self.db.lock:
            df = self.store.df.copy()
        if len(df) and order in df.columns:
            ascending = not order.startswith("-")
            key = order.lstrip("-")
            df = df.sort_values(key, ascending=ascending)
        if limit:
            df = df.head(limit)
        rows = [jsonable(r) for r in df.to_dict(orient="records")]
        return rows

    async def count(self, predicate: Callable[[dict], bool] | None = None) -> int:
        rows = await self.all()
        if predicate is None:
            return len(rows)
        return sum(1 for r in rows if predicate(r))

    async def get(self, row_id: int) -> dict | None:
        async with self.db.lock:
            df = self.store.df
        if not len(df):
            return None
        match = df[df["id"] == row_id]
        if not len(match):
            return None
        return jsonable(match.iloc[0].to_dict())

    async def find(
        self, predicate: Callable[[dict], bool], limit: int | None = None
    ) -> list[dict]:
        rows = await self.all()
        out = [r for r in rows if predicate(r)]
        return out[:limit] if limit else out

    async def next_id(self) -> int:
        async with self.db.lock:
            df = self.store.df
        if not len(df):
            return 1
        return int(df["id"].max()) + 1

    async def insert(self, **fields: Any) -> dict:
        async with self.db.lock:
            df = self.store.df.copy()
            if "id" not in fields:
                fields["id"] = (int(df["id"].max()) + 1) if len(df) else 1
            elif len(df) and fields["id"] <= int(df["id"].max()):
                fields["id"] = int(df["id"].max()) + 1
            row = {c: v for c, v in fields.items() if c in self.store.schema}
            if "id" not in row:
                row["id"] = fields["id"]
            new_df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            # keep dtypes sane for int columns (concat may upcast to object)
            for col, kind in self.store.schema.items():
                if kind == "int":
                    new_df[col] = pd.to_numeric(new_df[col], errors="coerce").astype(
                        "Int64"
                    )
            self.store.save(new_df, backup=False)
            return jsonable(row | {"id": row.get("id")})

    async def update(self, row_id: int, **fields: Any) -> dict | None:
        result: dict | None = None

        def _do(df: pd.DataFrame) -> dict | None:
            if not len(df):
                return None
            mask = df["id"] == row_id
            if not mask.any():
                return None
            for col, val in fields.items():
                if col in df.columns and col != "id":
                    df.loc[mask, col] = val
            if "updated_at" in df.columns:
                df.loc[mask, "updated_at"] = utcnow_iso()
            self.store.save(df, backup=False)
            return jsonable(df.loc[mask].iloc[0].to_dict())

        result = await self._with_lock(_do)
        return result

    async def update_where(
        self, predicate: Callable[[dict], bool], **fields: Any
    ) -> int:
        updated = 0

        def _do(df: pd.DataFrame) -> int:
            nonlocal updated
            if not len(df):
                return 0
            mask = pd.Series(
                [predicate(jsonable(row)) for _, row in df.iterrows()], index=df.index
            )
            if not mask.any():
                return 0
            for col, val in fields.items():
                if col in df.columns:
                    df.loc[mask, col] = val
            if "updated_at" in df.columns:
                df.loc[mask, "updated_at"] = utcnow_iso()
            self.store.save(df, backup=False)
            return int(mask.sum())

        updated = await self._with_lock(_do)
        return updated

    async def delete(self, row_id: int) -> bool:
        deleted = False

        def _do(df: pd.DataFrame) -> bool:
            nonlocal deleted
            if not len(df):
                return False
            mask = df["id"] == row_id
            if not mask.any():
                return False
            df = df[~mask].reset_index(drop=True)
            self.store.save(df, backup=True)
            return True

        deleted = await self._with_lock(_do)
        return deleted


class EventsRepository(Repository):
    async def add(
        self,
        event_type: str,
        message: str,
        pdf_id: int | None = None,
        user_id: int | None = None,
        status: str | None = None,
        evidence_type: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        from ..utils import json_dumps

        return await self.insert(
            pdf_id=pdf_id,
            user_id=user_id,
            event_type=event_type,
            status=status,
            message=message,
            evidence_type=evidence_type,
            metadata=json_dumps(metadata) if metadata else "",
            created_at=utcnow_iso(),
        )

    async def for_pdf(self, pdf_id: int, limit: int = 100) -> list[dict]:
        async with self.db.lock:
            df = self.store.df.copy()
        if not len(df):
            return []
        df = df[df["pdf_id"] == pdf_id].sort_values("id", ascending=False)
        rows = [jsonable(r) for r in df.to_dict(orient="records")]
        for r in rows:
            r["metadata"] = json_loads(r.get("metadata"), {})
        return rows[:limit]

    async def recent(self, limit: int = 50) -> list[dict]:
        rows = await self.all(limit=limit, order="-id")
        for r in rows:
            r["metadata"] = json_loads(r.get("metadata"), {})
        return rows


class SettingsRepository(Repository):
    async def get_key(self, key: str) -> dict | None:
        rows = await self.find(lambda r: r.get("key") == key)
        return rows[0] if rows else None

    async def set_key(self, key: str, value: str) -> dict:
        existing = await self.get_key(key)
        if existing:
            return await self.update(existing["id"], value=str(value))
        return await self.insert(key=key, value=str(value), updated_at=utcnow_iso())

    async def as_dict(self) -> dict[str, str]:
        rows = await self.all()
        return {r["key"]: r["value"] for r in rows if r.get("key")}


# ---------------------------------------------------------------------------
# Runtime settings (whitelisted admin overrides on top of env config)
# ---------------------------------------------------------------------------

# key -> (parser, env-default getter)
RUNTIME_SETTING_KEYS: dict[str, tuple[Callable[[str], Any], str]] = {
    "app_name": (str, "app_name"),
    "public_base_url": (str, "public_base_url"),
    "session_lifetime_hours": (int, "session_lifetime_hours"),
    "max_pdf_size_mb": (int, "max_pdf_size_mb"),
    "http_timeout": (int, "http_timeout"),
    "max_redirects": (int, "max_redirects"),
    "indexer_concurrency": (int, "indexer_concurrency"),
    "max_retries": (int, "max_retries"),
    "polling_interval_ms": (int, "polling_interval_ms"),
    "rss_enabled": (lambda v: str(v).lower() in ("1", "true", "yes"), "rss_enabled"),
    "sitemap_enabled": (lambda v: str(v).lower() in ("1", "true", "yes"), "sitemap_enabled"),
    "google_search_console_enabled": (
        lambda v: str(v).lower() in ("1", "true", "yes"),
        "google_search_console_enabled",
    ),
    "bing_webmaster_enabled": (
        lambda v: str(v).lower() in ("1", "true", "yes"),
        "bing_webmaster_enabled",
    ),
    "observation_enabled": (
        lambda v: str(v).lower() in ("1", "true", "yes"),
        "observation_enabled",
    ),
}


async def effective_setting(db: Database, key: str) -> Any:
    """Runtime DB override if present and valid, otherwise env config."""
    if key not in RUNTIME_SETTING_KEYS:
        raise DatabaseError(f"Setting '{key}' is not a runtime-configurable key")
    parser, env_key = RUNTIME_SETTING_KEYS[key]
    repo = SettingsRepository(db, "settings")
    row = await repo.get_key(key)
    if row and row.get("value") not in (None, ""):
        try:
            return parser(row["value"])
        except (ValueError, TypeError):
            log.warning("Invalid stored setting %s=%r — using env default", key, row["value"])
    return getattr(get_settings(), env_key)


def build_repositories(db: Database) -> dict[str, Repository]:
    return {
        "users": Repository(db, "users"),
        "pdfs": Repository(db, "pdfs"),
        "pages": Repository(db, "pages"),
        "jobs": Repository(db, "jobs"),
        "events": EventsRepository(db, "events"),
        "settings": SettingsRepository(db, "settings"),
        "integrations": Repository(db, "integrations"),
        "sessions": Repository(db, "sessions"),
    }


class Repos:
    """Convenience namespace for repositories bound to a Database."""

    def __init__(self, db: Database):
        self.db = db
        built = build_repositories(db)
        self.users: Repository = built["users"]
        self.pdfs: Repository = built["pdfs"]
        self.pages: Repository = built["pages"]
        self.jobs: Repository = built["jobs"]
        self.events: EventsRepository = built["events"]
        self.settings: SettingsRepository = built["settings"]
        self.integrations: Repository = built["integrations"]
        self.sessions: Repository = built["sessions"]


def get_repos(app_state) -> Repos:
    return app_state.repos
