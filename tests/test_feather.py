"""Feather persistence: atomicity, schema, backups, corruption, restart."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pandas as pd
import pytest

from app.database.feather_store import (
    Database,
    DatabaseCorruptedError,
    TableStore,
    reset_database_instance,
)
from app.utils import utcnow_iso


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    reset_database_instance()
    yield
    reset_database_instance()


class TestAtomicWrite:
    def test_create_missing_files(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        for name in ("users", "pdfs", "jobs", "events", "settings", "sessions", "integrations", "bot_indexer"):
            assert (tmp_path / f"{name}.feather").exists(), f"{name}.feather missing"

    def test_no_partial_files_after_writes(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        import asyncio

        async def write_stuff():
            store = db.table("events")
            for i in range(20):
                df = store.df
                import pandas as pd_

                new = pd_.concat(
                    [
                        df,
                        pd_.DataFrame(
                            [
                                {
                                    "id": i + 1,
                                    "pdf_id": None,
                                    "user_id": None,
                                    "event_type": "T",
                                    "status": "OK",
                                    "message": f"m{i}",
                                    "evidence_type": None,
                                    "metadata": "",
                                    "created_at": utcnow_iso(),
                                }
                            ]
                        ),
                    ],
                    ignore_index=True,
                )
                store.save(new)

        asyncio.new_event_loop().run_until_complete(write_stuff())
        leftovers = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob(".*.feather.tmp"))
        assert leftovers == [], f"temp files left: {leftovers}"

    def test_read_after_write(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("settings")
        df = store.df
        import pandas as pd_

        df = pd_.concat(
            [df, pd_.DataFrame([{"id": 1, "key": "k", "value": "v", "updated_at": utcnow_iso()}])],
            ignore_index=True,
        )
        store.save(df)
        # fresh store re-reads the file
        s2 = TableStore("settings", tmp_path)
        s2.init()
        assert s2.df.iloc[0]["value"] == "v"


class TestPersistenceAcrossRestart:
    def test_data_survives_new_database_instance(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("users")
        import pandas as pd_

        df = pd_.concat(
            [
                store.df,
                pd_.DataFrame(
                    [
                        {
                            "id": 1,
                            "name": "A",
                            "email": "a@e.com",
                            "password_hash": "x",
                            "role": "ADMIN",
                            "status": "ACTIVE",
                            "created_at": utcnow_iso(),
                            "updated_at": utcnow_iso(),
                            "last_login": None,
                            "created_by": None,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        store.save(df)

        # simulate a restart: brand-new Database instance on the same dir
        db2 = Database(tmp_path)
        db2.init()
        assert len(db2.table("users").df) == 1
        assert db2.table("users").df.iloc[0]["email"] == "a@e.com"


class TestBackups:
    def test_backup_created_on_delete(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("pdfs")
        import pandas as pd_

        # give it a row
        schema_row = {c: None for c in store.schema}
        schema_row.update({"id": 1, "user_id": 1, "original_url": "u", "normalized_url": "u", "url_hash": "h", "source_domain": "d", "status": "RECEIVED", "discovery_status": "DISCOVERY_PENDING", "crawl_status": "CRAWL_UNKNOWN", "index_status": "INDEX_UNKNOWN", "created_at": utcnow_iso(), "updated_at": utcnow_iso()})
        df = pd_.concat([store.df, pd_.DataFrame([schema_row])], ignore_index=True)
        store.save(df)

        backups_before = len(list(tmp_path.glob("backups/pdfs-*.feather")))
        store.save(store.df.iloc[0:0].reset_index(drop=True), backup=True)
        backups_after = len(list(tmp_path.glob("backups/pdfs-*.feather")))
        assert backups_after == backups_before + 1

    def test_restore_roundtrip(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("settings")
        import pandas as pd_

        df = pd_.concat(
            [store.df, pd_.DataFrame([{"id": 1, "key": "a", "value": "1", "updated_at": utcnow_iso()}])],
            ignore_index=True,
        )
        store.save(df)
        backup_path = store.backup(when="test")
        assert backup_path and backup_path.exists()

        # modify then restore
        df2 = pd_.concat(
            [store.df, pd_.DataFrame([{"id": 2, "key": "b", "value": "2", "updated_at": utcnow_iso()}])],
            ignore_index=True,
        )
        store.save(df2)
        assert len(store.df) == 2
        store.restore(backup_path)
        assert len(store.df) == 1


class TestCorruption:
    def test_corrupted_file_raises_clear_error(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        # corrupt the file
        (tmp_path / "users.feather").write_bytes(b"this is not feather data at all")
        with pytest.raises(DatabaseCorruptedError) as excinfo:
            db2 = Database(tmp_path)
            db2.init()
        msg = str(excinfo.value)
        assert "corrupted" in msg.lower()
        assert "NOT deleted" in msg
        assert "users" in msg

    def test_corruption_does_not_delete_file(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        (tmp_path / "pdfs.feather").write_bytes(b"garbage")
        with pytest.raises(DatabaseCorruptedError):
            Database(tmp_path).init()
        # original (corrupted) file untouched
        assert (tmp_path / "pdfs.feather").read_bytes() == b"garbage"

    def test_corrupted_file_with_backup_mention(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("jobs")
        import pandas as pd_

        row = {c: None for c in store.schema}
        row.update({"id": 1, "pdf_id": None, "job_type": "PIPELINE", "payload": "", "priority": 5, "status": "PENDING", "attempts": 0, "max_attempts": 3, "error": None, "started_at": None, "completed_at": None, "created_at": utcnow_iso()})
        store.save(pd_.concat([store.df, pd_.DataFrame([row])], ignore_index=True))
        store.backup(when="test")
        (tmp_path / "jobs.feather").write_bytes(b"corrupt!")
        with pytest.raises(DatabaseCorruptedError) as excinfo:
            Database(tmp_path).init()
        assert "backup" in str(excinfo.value).lower() or "backups" in str(excinfo.value).lower()


class TestSchemaMigration:
    def test_missing_columns_added_on_init(self, tmp_path):
        db = Database(tmp_path)
        db.init()
        store = db.table("users")
        import pandas as pd_

        row = {c: None for c in store.schema}
        row.update({"id": 1, "name": "A", "email": "a@e.com", "password_hash": "x", "role": "ADMIN", "status": "ACTIVE", "created_at": utcnow_iso(), "updated_at": utcnow_iso()})
        store.save(pd_.concat([store.df, pd_.DataFrame([row])], ignore_index=True))

        # simulate an OLD file: drop two columns that the current code expects
        df = pd.read_feather(tmp_path / "users.feather")
        df = df.drop(columns=["last_login", "created_by"])
        df.to_feather(tmp_path / "users.feather")

        db2 = Database(tmp_path)
        db2.init()
        cols = db2.table("users").df.columns
        assert "last_login" in cols
        assert "created_by" in cols
        # data preserved
        assert db2.table("users").df.iloc[0]["email"] == "a@e.com"
        # a backup was made for the migration
        assert len(list(tmp_path.glob("backups/users-*.feather"))) >= 1
