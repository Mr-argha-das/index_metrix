"""Feather persistence layer.

Feather (``pandas`` + ``pyarrow``) is file-oriented and **not** a transactional
database, so this module implements a safe data-access contract on top of it:

* every table is a ``.feather`` file under ``data/``
* a single in-process ``asyncio.Lock`` serialises read-modify-write cycles so
  concurrent workers can never interleave writes
* writes are atomic: data is written to a temp file in the same directory,
  fsync'ed, then ``os.replace``'d over the target — a partially written
  Feather file can never be observed
* a timestamped backup is kept in ``data/backups/`` before destructive saves
  and schema migrations
* schema validation on load: missing columns are added (migration), and a
  corrupted file raises :class:`DatabaseCorruptedError` with a clear message
  instead of silently deleting data
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from ..utils import utcnow_iso

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# column -> "int" | "float" | "bool" | "str"
TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    # catalog lives in the primary database file data/bot_indexer.feather
    "bot_indexer": {
        "key": "str",
        "value": "str",
        "updated_at": "str",
    },
    "users": {
        "id": "int",
        "name": "str",
        "email": "str",
        "password_hash": "str",
        "role": "str",
        "status": "str",
        "created_at": "str",
        "updated_at": "str",
        "last_login": "str",
        "created_by": "int",
    },
    "pdfs": {
        "id": "int",
        "user_id": "int",
        "original_url": "str",
        "normalized_url": "str",
        "url_hash": "str",
        "source_domain": "str",
        "final_url": "str",
        "status": "str",
        "http_status": "float",
        "content_type": "str",
        "content_length": "float",
        "sha256": "str",
        "page_count": "float",
        "title": "str",
        "author": "str",
        "subject": "str",
        "creator": "str",
        "producer": "str",
        "created_date": "str",
        "modified_date": "str",
        "text_length": "float",
        "first_page_text": "str",
        "language": "str",
        "classification": "str",
        "error": "str",
        "discovery_status": "str",
        "crawl_status": "str",
        "index_status": "str",
        "index_evidence": "str",
        "crawl_evidence": "str",
        "submission_status": "str",
        "discovery_channels": "str",
        "validated_at": "str",
        "last_checked_at": "str",
        "robots_check": "str",
        "html_metadata": "str",
        "resource_type": "str",
        "redirect_chain": "str",
        "declared_content_length": "float",
        "reference_submission_status": "str",
        "reference_submission_result": "str",
        "reference_crawl_status": "str",
        "reference_crawl_evidence": "str",
        "reference_index_status": "str",
        "reference_last_checked_at": "str",
        "external_discovery_status": "str",
        "external_discovery_evidence": "str",
        "external_crawl_status": "str",
        "external_crawl_evidence": "str",
        "external_index_status": "str",
        "source_index_status": "str",
        "source_index_evidence": "str",
        "last_probe_at": "str",
        "last_probe_status": "str",
        "created_at": "str",
        "updated_at": "str",
    },
    "pages": {
        "id": "int",
        "pdf_id": "int",
        "slug": "str",
        "page_url": "str",
        "pdf_sha256": "str",
        "page_kind": "str",
        "job_number": "int",
        "demo_job": "str",
        "title": "str",
        "description": "str",
        "published_at": "str",
        "updated_at": "str",
        "sitemap_included": "bool",
        "rss_included": "bool",
    },
    "jobs": {
        "id": "int",
        "pdf_id": "float",
        "job_type": "str",
        "payload": "str",
        "priority": "int",
        "status": "str",
        "attempts": "int",
        "max_attempts": "int",
        "next_attempt_at": "str",
        "error": "str",
        "started_at": "str",
        "completed_at": "str",
        "created_at": "str",
    },
    "events": {
        "id": "int",
        "pdf_id": "float",
        "user_id": "float",
        "event_type": "str",
        "status": "str",
        "message": "str",
        "evidence_type": "str",
        "metadata": "str",
        "created_at": "str",
    },
    "integrations": {
        "id": "int",
        "provider": "str",
        "property_url": "str",
        "status": "str",
        "credentials_reference": "str",
        "authorized_sites": "str",
        "last_check_at": "str",
        "last_check_message": "str",
        "created_at": "str",
        "updated_at": "str",
    },
    "settings": {
        "id": "int",
        "key": "str",
        "value": "str",
        "updated_at": "str",
    },
    "sessions": {
        "id": "int",
        "token_hash": "str",
        "user_id": "int",
        "created_at": "str",
        "expires_at": "str",
        "last_seen_at": "str",
    },
}

# Table -> file name. The catalog table lives in the primary database file.
TABLE_FILES: dict[str, str] = {
    "bot_indexer": "bot_indexer.feather",
    "users": "users.feather",
    "pdfs": "pdfs.feather",
    "jobs": "jobs.feather",
    "events": "events.feather",
    "settings": "settings.feather",
    "integrations": "integrations.feather",
    "sessions": "sessions.feather",
}


class DatabaseError(RuntimeError):
    """Base error for database problems."""


class DatabaseCorruptedError(DatabaseError):
    """A Feather file could not be read (corruption / wrong schema)."""

    def __init__(self, table: str, path: str, reason: str, backups: list[str] | None = None):
        msg = (
            f"Feather database file for table '{table}' is corrupted and could "
            f"not be read: {reason}. File: {path}. User data was NOT deleted."
        )
        if backups:
            msg += (
                " Recent backups available: "
                + ", ".join(backups)
                + ". Restore one with: python scripts/restore.py --table "
                + table
                + " --backup <file>"
            )
        super().__init__(msg)
        self.table = table
        self.path = path
        self.reason = reason
        self.backups = backups or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_df(schema: dict[str, str]) -> pd.DataFrame:
    cols: dict[str, Any] = {}
    for col, kind in schema.items():
        if kind == "int":
            cols[col] = pd.Series([], dtype="int64")
        elif kind == "float":
            cols[col] = pd.Series([], dtype="float64")
        elif kind == "bool":
            cols[col] = pd.Series([], dtype="bool")
        else:
            cols[col] = pd.Series([], dtype="object")
    return pd.DataFrame(cols)


def _coerce_row(schema: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a row into the table's column types (unknown keys ignored)."""
    out: dict[str, Any] = {}
    for col, kind in schema.items():
        if col not in row:
            continue
        value = row[col]
        if value is None:
            out[col] = None if kind != "bool" else False
            continue
        if kind == "int":
            out[col] = int(value)
        elif kind == "float":
            try:
                out[col] = float(value)
            except (TypeError, ValueError):
                out[col] = None
        elif kind == "bool":
            out[col] = bool(value)
        else:
            out[col] = str(value)
    return out


def _rows_from_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = df.to_dict(orient="records")
    out = []
    for rec in records:
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float) and pd.isna(v):
                clean[k] = None
            elif isinstance(v, (int,)) and not isinstance(v, bool):
                clean[k] = int(v)
            else:
                clean[k] = v
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
# TableStore
# ---------------------------------------------------------------------------

class TableStore:
    """One Feather file + an in-memory DataFrame cache."""

    def __init__(self, name: str, data_dir: Path, schema: dict[str, str] | None = None):
        self.name = name
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self.schema = schema if schema is not None else TABLE_SCHEMAS[name]
        self.file_name = TABLE_FILES.get(name, f"{name}.feather")
        self.path = data_dir / self.file_name
        self._df: pd.DataFrame | None = None
        self.schema_version: int = 1

    # -- lifecycle ---------------------------------------------------------

    def init(self) -> None:
        """Load (or create) the file and validate/migrate the schema."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._df = _empty_df(self.schema)
            self._write(self._df, backup=False)
            return
        try:
            df = pd.read_feather(self.path)
        except Exception as exc:  # noqa: BLE001 - report, never delete
            raise DatabaseCorruptedError(
                self.name, str(self.path), str(exc), self._backup_names()
            ) from exc
        # Schema migration: add any missing columns.
        missing = [c for c in self.schema if c not in df.columns]
        if missing:
            df = self._migrate(df, missing)
        # Drop columns we no longer define (keep a backup first!).
        extra = [c for c in df.columns if c not in self.schema]
        if extra:
            df = self._migrate(df, [], drop=extra)
        self._df = df

    def _migrate(self, df: pd.DataFrame, add: list[str], drop: list[str] | None = None) -> pd.DataFrame:
        self.backup(when="migration")
        import numpy as np

        for col in add:
            kind = self.schema[col]
            if kind == "int":
                df[col] = pd.Series([pd.NA] * len(df), dtype="Int64", index=df.index)
            elif kind == "float":
                df[col] = pd.Series([np.nan] * len(df), dtype="float64", index=df.index)
            elif kind == "bool":
                df[col] = pd.Series([False] * len(df), dtype="bool", index=df.index)
            else:
                df[col] = pd.Series([None] * len(df), dtype="object", index=df.index)
        for col in drop or []:
            df = df.drop(columns=[col])
        self._write(df, backup=False)
        return df

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            self.init()
        return self._df

    @df.setter
    def df(self, value: pd.DataFrame) -> None:
        self._df = value

    # -- atomic IO -----------------------------------------------------------

    def _write(self, df: pd.DataFrame, backup: bool = False) -> None:
        if backup:
            self.backup(when="save")
        tmp = self.path.with_name(
            f".{self.name}.{uuid.uuid4().hex[:8]}.feather.tmp"
        )
        try:
            df.to_feather(str(tmp))
            # fsync the temp file so the bytes are durable before rename
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self._df = df
            # fsync the directory so the rename is durable
            try:
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def save(self, df: pd.DataFrame, backup: bool = False) -> None:
        """Persist a (possibly modified) DataFrame atomically."""
        self._write(df, backup=backup)

    # -- backups -------------------------------------------------------------

    def _backup_names(self) -> list[str]:
        if not self.backup_dir.exists():
            return []
        prefix = f"{self.name}-"
        return sorted(
            p.name for p in self.backup_dir.glob(prefix + "*.feather")
        )[-5:]

    def backup(self, when: str = "manual") -> Path | None:
        """Copy the current file into data/backups/. Returns the backup path."""
        if not self.path.exists() or not self.path.stat().st_size:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backup_dir / f"{self.name}-{stamp}-{when}.feather"
        n = 1
        while dest.exists():
            dest = self.backup_dir / f"{self.name}-{stamp}-{when}-{n}.feather"
            n += 1
        shutil.copy2(self.path, dest)
        # prune: keep the 20 most recent backups per table
        existing = sorted(
            self.backup_dir.glob(f"{self.name}-*.feather"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in existing[20:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest

    def latest_backup(self) -> Path | None:
        names = self._backup_names()
        if not names:
            return None
        return self.backup_dir / names[-1]

    def restore(self, backup_path: Path) -> None:
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise DatabaseError(f"Backup file not found: {backup_path}")
        # Verify the backup is readable before touching the live file.
        try:
            df = pd.read_feather(backup_path)
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(
                f"Backup file is itself unreadable: {exc}"
            ) from exc
        self._write(df, backup=True)

    def health(self) -> dict:
        try:
            _ = self.df
            rows = 0 if self._df is None else len(self._df)
            return {"table": self.name, "rows": rows, "ok": True}
        except DatabaseError as exc:
            return {"table": self.name, "ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Database (registry + lock)
# ---------------------------------------------------------------------------

class Database:
    """Registry of all table stores with a single in-process write lock."""

    _instance: "Database | None" = None

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.lock = asyncio.Lock()
        self.tables: dict[str, TableStore] = {
            name: TableStore(name, self.data_dir) for name in TABLE_SCHEMAS
        }
        Database._instance = self

    @classmethod
    def instance(cls) -> "Database":
        if cls._instance is None:
            raise DatabaseError("Database not initialised — call Database(...) first")
        return cls._instance

    def init(self) -> None:
        """Load every table, validating/migrating schemas as needed."""
        for store in self.tables.values():
            store.init()
        # Record catalog metadata in the primary database file.
        catalog = self.tables["bot_indexer"]
        df = catalog.df
        data = {row["key"]: row["value"] for _, row in df.iterrows()} if len(df) else {}
        data.setdefault("schema_version", "1")
        data.setdefault("created_at", utcnow_iso())
        data["updated_at"] = utcnow_iso()
        catalog.df = pd.DataFrame(
            [{"key": k, "value": v, "updated_at": data["updated_at"]} for k, v in data.items()]
        )
        catalog.save(catalog.df, backup=False)

    def table(self, name: str) -> TableStore:
        return self.tables[name]

    def health(self) -> dict:
        return {
            "database": "ok",
            "tables": [store.health() for store in self.tables.values()],
        }


def reset_database_instance() -> None:
    """Test helper: drop the shared instance."""
    Database._instance = None
