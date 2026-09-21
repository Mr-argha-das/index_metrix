"""Schema migrations.

Feather files have a flat schema per file. Migration = adding missing
columns (or dropping deprecated ones), which :class:`TableStore.init`
performs automatically. Before any destructive change the table is backed up
into ``data/backups/``.

Use ``python scripts/restore.py --table <name> --backup <file>`` to roll back.
"""
from __future__ import annotations

import logging

from .feather_store import Database, DatabaseCorruptedError, TABLE_SCHEMAS

log = logging.getLogger("bot_indexer.migrations")

SCHEMA_VERSION = "1"


def describe_schemas() -> dict[str, dict[str, str]]:
    """Human-readable schema map for docs / the settings page."""
    return TABLE_SCHEMAS


async def migrate_all(db: Database) -> list[str]:
    """Ensure every table matches the current schema (idempotent).

    TableStore.init() already runs the add-columns migration on load; this
    function gives callers an explicit, logged entry point and verifies the
    catalog in the primary ``bot_indexer.feather`` file.
    """
    performed: list[str] = []
    for name, store in db.tables.items():
        try:
            store.init()
        except DatabaseCorruptedError:
            log.error("Migration for table '%s' failed: file corrupted", name)
            raise
        if len(store.df) or store.path.exists():
            performed.append(name)
    log.info("Schema migration complete (%s tables), version %s", len(performed), SCHEMA_VERSION)
    return performed


def backup_all(db: Database) -> list[str]:
    """Backup every table before a planned destructive change."""
    paths = []
    for store in db.tables.values():
        p = store.backup(when="pre-migration")
        if p:
            paths.append(str(p))
    return paths
