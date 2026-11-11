# Development log

Where the project stands, newest entry first. Each entry says what works, what
was decided and what comes next. Commit messages carry the same information in
short form, so `git log` reads as a history of the project's state.

## 2026-11-11: phase 0, scaffolding

**Works**

- Python package `atlas` under `src/`, standard library only.
- `atlas init` creates the SQLite file and applies migrations.
- Schema version 1: bookmarks, source records, tags, sync state, grooming
  journal, meta.

**Decided**

- Migrations are numbered SQL files in `src/atlas/migrations`. The schema
  version lives in `meta.schema_version`. Commands refuse to run on an outdated
  database and say to run `atlas init`.
- Opening a database that does not exist is an error, so a typo in `--db`
  cannot silently start an empty collection.
- Analysis and saved-view tables are not created yet. They arrive as their own
  migrations with the phases that use them.
- `source_records` gained a `raw_hash` column compared with the first plan, to
  recognise an unchanged record on re-import.
- No continuous integration yet. Tests run locally with `pytest`.

**Next**

- Phase 1: CSV import, then export and status.
