# SQLite schema migration plan

The current database bootstrap is intentionally idempotent but is not a versioned
migration system. Existing installations may therefore drift from a fresh database
when table definitions, indexes, triggers, or data backfills change.

The repository now has a deliberately small migration foundation. Version 1 records
the existing additive bootstrap as a baseline in `schema_migration`; startup validates
the ordered history and checksums before running compatibility DDL, and refuses a
database newer than the application. This baseline does not synthesize business data.

The remaining steps before the first statement-bearing migration are:

1. Move future changes into numbered, append-only `SchemaMigration` entries.
2. Execute statement-bearing migrations in one transaction after making an integrity-checked
   backup; record version, applied time, application version, and checksum.
3. Refuse startup when a known migration checksum changes or the database version is
   newer than the application.
4. Test both an empty database and a copy of every supported historical schema.
5. Keep destructive column/table changes in separate releases with explicit rollback
   or restore instructions.

Until backup/restore handling is implemented, schema edits must remain additive and
statement-bearing migrations should not be released. In particular,
`engineering_project.lifecycle_stage` is a deprecated compatibility field: the
read-only lifecycle aggregator derives its result from canonical events and never
writes that column.
