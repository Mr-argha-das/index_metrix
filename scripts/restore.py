#!/usr/bin/env python
"""Restore a Feather table from a backup file.

    python scripts/restore.py --table pdfs
    python scripts/restore.py --table pdfs --backup data/backups/pdfs-20260920-120000-save.feather

The live file is itself backed up before being replaced.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.feather_store import Database, DatabaseCorruptedError, TABLE_SCHEMAS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a Feather table from backup")
    parser.add_argument("--table", required=True, choices=list(TABLE_SCHEMAS.keys()))
    parser.add_argument("--backup", default=None, help="specific backup file (default: latest)")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.data_dir)
    store = db.table(args.table)

    backup = Path(args.backup) if args.backup else store.latest_backup()
    if not backup or not backup.exists():
        print("No backup file found for this table.")
        return 1

    try:
        store.restore(backup)
    except Exception as exc:  # noqa: BLE001
        print(f"Restore failed: {exc}")
        return 1
    print(f"Restored table '{args.table}' from {backup}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DatabaseCorruptedError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
