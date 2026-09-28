# SQLite schema migration plan

The current database bootstrap is intentionally idempotent but is not a versioned
migration system. Existing installations may therefore drift from a fresh database
when table definitions, indexes, triggers, or data backfills change.

This change set does **not** introduce a migration framework because doing so safely
requires a dedicated release and recovery test. The proposed minimum foundation is:

1. Add a single-row `schema_version` table and numbered, append-only migration files.
2. Execute pending migrations in one transaction after making an integrity-checked
   backup; record version, applied time, application version, and checksum.
3. Refuse startup when a known migration checksum changes or the database version is
   newer than the application.
4. Test both an empty database and a copy of every supported historical schema.
5. Keep destructive column/table changes in separate releases with explicit rollback
   or restore instructions.

Until that work is implemented, schema edits must remain additive and startup must not
silently reinterpret existing columns. In particular,
`engineering_project.lifecycle_stage` is a deprecated compatibility field: the
read-only lifecycle aggregator derives its result from canonical events and never
writes that column.
