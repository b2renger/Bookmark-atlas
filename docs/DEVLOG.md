# Development log

Where the project stands, newest entry first. Each entry says what works, what
was decided and what comes next. Commit messages carry the same information in
short form, so `git log` reads as a history of the project's state.

## 2026-11-11: first usable build, local interface

**Works**

- `atlas ui` starts a local web page and opens it in the browser.
- Settings: token field (checked with Notion before it is stored), list of
  tables, column mapping with a guess, Preview and Import, report.
- Bookmarks: list with search over titles, summaries, links and tags.
- The page reopens on the selected table and mapping after a reload.

**Tested how**

- Server and API: automated tests (`tests/test_server.py`).
- The page itself: driven in Chrome through the whole flow against the fake
  Notion: wrong token, right token, list, pick, bad mapping, preview, import,
  second import, list, show more, search, reload. Light and dark, wide and
  420 px wide. This browser run is not part of the test suite yet.
- Bookmark text containing HTML is shown as text, and a `javascript:` link is
  refused at import.
- Still not run against the real Notion API.

**Decided**

- The first build is a local web interface served by the Python package, not
  the Tauri shell. It needs only Python to run, and the same page will be
  wrapped by the desktop shell later. Tauri moves from "phase 4 prerequisite"
  to "packaging step".
- The page calls a JSON API under `/api`; each call lands in the same functions
  as the command line.
- The server listens on 127.0.0.1 only, refuses requests addressed to any
  other host name, and requires a random per-run token on every API call, so
  other web pages open in the browser cannot reach it. A content security
  policy forbids loading anything from elsewhere.
- The API never sends the Notion token back, only a masked hint.
- `atlas ui` creates the database if it does not exist. Other commands still
  refuse a missing database.
- Help text names both token kinds Notion offers: personal access token (no
  sharing step) and internal connection token (database must be shared).

**Known gaps**

- No review screen yet for records waiting for review. `atlas status` and the
  header show the count.
- No CSV import from the interface, only from the command line.
- Imports run inside the request. Fine for a thousand rows; a progress display
  is needed before much larger tables.
- No continuous integration.

**Next**

- Run Preview and Import against the real table and compare with the CSV
  profile (956 bookmarks expected).
- Phase 2 grooming.

## 2026-11-11: phase 1, Notion connection and sync

**Works**

- `atlas notion token`: asks for the token, checks it with Notion, stores it.
- `atlas notion tables`: lists the tables shared with the connection.
- `atlas notion select <id>`: chooses the table and guesses which column feeds
  which field (overridable with `--column`).
- `atlas sync [--full] [--dry-run]`: imports rows. The first run reads the
  whole table; later runs ask only for rows edited since the last one.

**Tested how**

- Against an in-memory fake of the Notion API (`tests/fake_notion.py`) whose
  answers follow the reference documentation read on 2026-11-11.
- **Not yet run against the real Notion API.** No token was available. The
  first real run is the actual test, and `--dry-run` exists for it.

**Decided**

- The token never goes into the database. Lookup order: `NOTION_TOKEN`
  environment variable, system keychain if the optional `keyring` package is
  installed, otherwise `secrets.json` in the user's configuration folder,
  created readable by the owner only.
- A token is checked with Notion before it is stored.
- Schema v1 was edited in place: `source_records.raw_hash` became
  `content_hash`, a hash of the mapped fields (URL, title, summary, sorted
  tags, parsed date). No database existed outside development, so no migration
  was written. From now on schema changes are new migration files.
- The content hash is what makes CSV-then-Notion work: a Notion page whose
  content equals a row already imported from CSV is linked quietly, with
  nothing to review.
- Rows are matched by Notion page ID first, then by normalised URL.
- A row edited in Notion updates its source record and waits for review. The
  bookmark is not touched.
- For Notion rows, `saved_at` is the page creation time (source
  `notion_created`). An empty `saved_at` on an existing bookmark is filled the
  same way and journaled in `changes`; a filled one is never replaced.
- The sync report says on how many rows the page creation time matches the
  Date column. That number tells whether creation time can be trusted as the
  save time (see "Not yet verified" in the plan).
- Rows in Notion's trash are ignored for now. Detecting removals is left for a
  later `--full` pass.
- If Notion says a listing is incomplete, the sync fails and writes nothing.
- Requests are spaced 0.35 s apart; 429, 503 and 529 answers are retried after
  the `Retry-After` delay, up to five attempts.

**Next**

- A first interface: Settings with the token field and table picker.

## 2026-11-11: phase 1, export and status

**Works**

- `atlas export --format csv|json [-o FILE] [--include-deleted]`.
- `atlas status`: counts of bookmarks, deleted, source records, records waiting
  for review, tags, and fields still empty.
- README explains how to run everything so far.

**Checked on the real export**

- CSV and JSON exports both hold 956 bookmarks, in the same order, with the
  same tags.
- Status after import: 6 records waiting for review, 1,843 of 2,368 tags used
  only once, 439 bookmarks without a save date, none with a publication date or
  platform yet. These are the numbers phase 2 should move.

**Decided**

- Exports contain curated fields only: no source records, no journal.
- In CSV, tags share one column joined by ", ", as Notion does. In JSON they
  are a list.
- Deleted and merged bookmarks are left out unless `--include-deleted` is given.

**Not done in phase 1 yet**

- Notion sync (`atlas connect`, `atlas sync`). It needs a Notion connection
  token and the table shared with it, and should be tested against the real
  API, not only against fakes.

**Next**

- Either the Notion sync, or phase 2 grooming on the CSV-imported data. The
  two are independent.

## 2026-11-11: phase 1, CSV import

**Works**

- `atlas import-csv <file>` with `--dry-run` and `--column field=header`.
- Rows are matched to bookmarks by normalised URL. New links become bookmarks;
  links already known are stored as source records and never overwrite
  anything.
- Importing the same file twice writes nothing.
- A bookmark deleted locally stays deleted when its link is imported again.
- A whole file is one transaction: it all succeeds or nothing is written.

**Checked on the real export (962 rows)**

- 956 bookmarks created, 6 rows recognised as duplicates and held for review.
  Three of them have identical URLs. The other three were only found through
  normalisation: a tracking parameter, a YouTube share parameter, and the same
  X post under two account paths. All six are true duplicates.
- 439 dates have no time of day and were left empty on purpose.
- A second import reported 962 rows already imported and created nothing.

**Decided**

- Exact duplicates collapse at import, because the normalised URL is the only
  key a CSV row has. The first row creates the bookmark; later rows for the
  same link are kept as pending source records. Their tags are not merged in
  automatically.
- A date with a time of day goes to `saved_at` (source `date_column`). A date
  without one is ambiguous (save day or publication day), so both date fields
  stay empty and the original value remains in the source record for the
  `split-dates` script.
- `platform` is not filled at import. It is the job of `derive-platform`.
- Tags are stored exactly as written. Normalising them is grooming.
- URL rules so far: scheme, `www.`, fragment, trailing slash and tracking
  parameters are ignored; twitter.com is x.com; an X post is identified by its
  ID alone; a YouTube video by its `v` parameter; GitHub owner and repository
  names are case-insensitive.

**Next**

- `atlas export` and `atlas status`, then the Notion sync.

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
