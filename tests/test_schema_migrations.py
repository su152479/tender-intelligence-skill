import sqlite3

import pytest

from opportunity_radar.db import Database
from opportunity_radar.schema_migrations import (
    CURRENT_SCHEMA_VERSION, SCHEMA_MIGRATIONS, SchemaMigration, SchemaMigrationError,
    apply_schema_migrations,
)
import opportunity_radar.schema_migrations as schema_migrations


def _migration_rows(db):
    with db.connect() as con:
        return con.execute(
            "SELECT version,name,checksum FROM schema_migration ORDER BY version"
        ).fetchall()


def test_fresh_database_records_current_schema_baseline(tmp_path):
    db = Database(tmp_path / "fresh.db")
    db.init()

    rows = _migration_rows(db)
    assert rows == [(
        CURRENT_SCHEMA_VERSION, SCHEMA_MIGRATIONS[0].name,
        SCHEMA_MIGRATIONS[0].checksum,
    )]
    with db.connect() as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project'"
        ).fetchone()


def test_legacy_unversioned_database_is_baselined_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE project (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT NOT NULL,
            project_no TEXT, publish_date TEXT, region TEXT, owner TEXT, tenderer TEXT,
            project_stage TEXT, construction_content TEXT, source_site TEXT NOT NULL,
            url TEXT NOT NULL, raw_text TEXT, ai_score INTEGER DEFAULT 0,
            matched_products TEXT DEFAULT '[]', analysis_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_site,url)
        )""")
        con.execute(
            "INSERT INTO project(project_name,source_site,url) VALUES('历史公告','旧来源','https://example.test/1')"
        )

    db = Database(path)
    db.init()

    with db.connect() as con:
        assert con.execute("SELECT project_name FROM project").fetchone()[0] == "历史公告"
        columns = {row[1] for row in con.execute("PRAGMA table_info(project)")}
        assert {"analyzer_version", "notice_actionability"} <= columns
    assert len(_migration_rows(db)) == 1


def test_schema_baseline_is_idempotent(tmp_path):
    db = Database(tmp_path / "idempotent.db")
    db.init()
    first = _migration_rows(db)
    db.init()
    assert _migration_rows(db) == first


def test_newer_database_is_rejected_before_bootstrap_mutation(tmp_path):
    path = tmp_path / "newer.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE schema_migration (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute(
            "INSERT INTO schema_migration(version,name,checksum) VALUES(999,'future','future')"
        )

    with pytest.raises(SchemaMigrationError, match="高于程序支持版本"):
        Database(path).init()

    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project'"
        ).fetchone() is None


def test_modified_migration_checksum_is_rejected(tmp_path):
    path = tmp_path / "tampered.db"
    migration = SCHEMA_MIGRATIONS[0]
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE schema_migration (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute(
            "INSERT INTO schema_migration(version,name,checksum) VALUES(?,?,?)",
            (migration.version, migration.name, "0" * 64),
        )

    with pytest.raises(SchemaMigrationError, match="校验和"):
        Database(path).init()


def test_statement_migration_is_blocked_until_backup_flow_exists(tmp_path, monkeypatch):
    path = tmp_path / "blocked-ddl.db"
    pending = SchemaMigration(2, "unsafe-without-backup", ("CREATE TABLE unsafe(id INTEGER)",))
    monkeypatch.setattr(
        schema_migrations, "SCHEMA_MIGRATIONS", (*SCHEMA_MIGRATIONS, pending),
    )

    with pytest.raises(SchemaMigrationError, match="备份"):
        with sqlite3.connect(path) as con:
            apply_schema_migrations(con)

    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unsafe'"
        ).fetchone() is None
