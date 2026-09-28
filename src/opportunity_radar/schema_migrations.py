"""Small, auditable SQLite schema-version foundation.

Version 1 records the existing additive bootstrap as a baseline. Future schema
changes belong here as ordered SQL statements instead of adding more implicit
startup mutations. Business data is never synthesized by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3


MIGRATION_TABLE_SCHEMA = """CREATE TABLE IF NOT EXISTS schema_migration (
 version INTEGER PRIMARY KEY,
 name TEXT NOT NULL,
 checksum TEXT NOT NULL,
 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""


class SchemaMigrationError(RuntimeError):
    """Raised when the database migration history cannot be trusted."""


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    statements: tuple[str, ...] = ()

    @property
    def checksum(self) -> str:
        payload = json.dumps(
            {"version": self.version, "name": self.name, "statements": self.statements},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SCHEMA_MIGRATIONS = (
    SchemaMigration(1, "baseline-current-additive-schema"),
)
CURRENT_SCHEMA_VERSION = SCHEMA_MIGRATIONS[-1].version


def _migration_table_exists(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
    ).fetchone() is not None


def _applied_rows(con: sqlite3.Connection) -> list[tuple[int, str, str]]:
    if not _migration_table_exists(con):
        return []
    try:
        return con.execute(
            "SELECT version,name,checksum FROM schema_migration ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SchemaMigrationError(f"无法读取 schema_migration：{exc}") from exc


def validate_schema_migrations(con: sqlite3.Connection) -> None:
    """Reject newer, unknown, gapped, or modified migration histories."""
    rows = _applied_rows(con)
    if not rows:
        return
    registry = {migration.version: migration for migration in SCHEMA_MIGRATIONS}
    versions = [int(row[0]) for row in rows]
    if max(versions) > CURRENT_SCHEMA_VERSION:
        raise SchemaMigrationError(
            f"数据库结构版本 {max(versions)} 高于程序支持版本 {CURRENT_SCHEMA_VERSION}"
        )
    expected_prefix = list(range(1, max(versions) + 1))
    if versions != expected_prefix:
        raise SchemaMigrationError(f"数据库迁移历史不连续：{versions}")
    for version, name, checksum in rows:
        migration = registry.get(int(version))
        if migration is None:
            raise SchemaMigrationError(f"数据库包含未知迁移版本：{version}")
        if name != migration.name or checksum != migration.checksum:
            raise SchemaMigrationError(
                f"迁移版本 {version} 的名称或校验和与当前程序不一致"
            )


def apply_schema_migrations(con: sqlite3.Connection) -> list[int]:
    """Apply missing registered migrations in the caller's transaction."""
    con.execute(MIGRATION_TABLE_SCHEMA)
    validate_schema_migrations(con)
    applied = {int(row[0]) for row in _applied_rows(con)}
    newly_applied = []
    for migration in SCHEMA_MIGRATIONS:
        if migration.version in applied:
            continue
        if migration.statements:
            raise SchemaMigrationError(
                f"迁移版本 {migration.version} 包含结构变更；"
                "在备份、完整性校验和恢复流程接入前禁止自动执行"
            )
        for statement in migration.statements:
            con.execute(statement)
        con.execute(
            "INSERT INTO schema_migration(version,name,checksum) VALUES(?,?,?)",
            (migration.version, migration.name, migration.checksum),
        )
        newly_applied.append(migration.version)
    return newly_applied
