# Implementation plan

First written 2026-11-11, revised the same day after reading a real export of
the Notion table. Facts marked "checked" were read at the source on that date;
re-check them before relying on them.

## 1. Principles

1. **Atlas is the final database.** The local SQLite file is the system of
   record. Notion is only a place where data comes in (inbox) and, optionally,
   goes out (outbox). Curated local data is never overwritten by an import.
2. **Grooming is a first-class feature.** Cleaning the collection (duplicates,
   tags, dates) is done by scripts that run from the interface, always with a
   preview, a journal and an undo.
3. **Universal format.** One SQLite file, exportable to CSV and JSON.
4. **Discovery over storage.** Views exist to reveal links, neighbours and
   trends, not only to list bookmarks.
5. **e-means is under test.** It is the default clustering engine only if it
   holds up against classic k-means on this data.

Not goals for now: capturing new bookmarks, generating summaries or tags (an
existing tool does this), mobile, multi-user sync.

## 2. What the real data looks like

From a CSV export of the table read on 2026-11-11 (aggregate figures only).

| Item | Finding |
| --- | --- |
| Size | 962 bookmarks |
| Columns | Name, Date, Description, Keywords, Platform, URL |
| Description | Always filled, median 270 characters, nearly all English |
| Keywords | Filled on 961 rows, median 5 per bookmark |
| Tag vocabulary | 2,376 distinct tags; 1,845 used once; 148 used 5 times or more |
| Tag duplicates | 64 groups differing only by case or hyphen, plus synonyms |
| Platform | Empty on 79% of rows |
| Domains | 214; x.com is 51%, github.com 19% |
| Duplicates | 3 repeated URLs, 6 repeated titles |
| Date | Mixed meaning, see below |

Consequences:

- Summary and tags are columns, so one query returns everything. No per-page
  body requests. A full import is about ten requests.
- Tags are too sparse to drive clustering or links on their own. Embeddings of
  the descriptions carry the analysis; tag clean-up comes before it.
- Platform is derived from the URL domain, not read from the column.
- At 962 points every clustering algorithm is instant. The e-means test is
  about cluster quality and stability, not speed.

### The Date column mixes two meanings

- 440 rows hold a date with no time of day (midnight UTC).
- For X posts the publication time can be computed from the post ID in the URL.
  All 491 X links carry one.
- 307 X rows are date-only, and 305 X rows have a Date in the 24 hours before
  the post's publication time: they hold the publication day, not the save
  time.
- The other X rows have a real time of day and fall after publication: those
  are save times.

So the column is split into two fields, `saved_at` and `published_at`.

## 3. Architecture

```
            in                                         out
Notion ──────────┐                              ┌──────────> Notion (optional)
CSV export ──────┴──> importer ──> SQLite <─────┴──────────> CSV / JSON
                                     ^   ^
                 grooming scripts ───┘   └─── analysis (embeddings, clusters)
                                     ^
                              desktop shell (Tauri) ──> views, grooming panel
                                     |
                                     └──> snapshot file / HTML export
```

| Part | Choice | Why |
| --- | --- | --- |
| Desktop shell | Tauri 2 | Small binaries, web frontend for visualisation |
| Frontend | TypeScript, framework chosen in phase 4 | Views are web-native |
| Data, grooming, analysis | Python package with a command line | e-means ships as Python in Faiss; scripts are easy to write |
| Storage | SQLite, one file | Readable from nearly any tool or language |
| Secrets | OS keychain | The Notion token never touches the data file |

Everything the interface does is a call to the Python package, so every
feature also works from the command line and can be tested without a UI.

### Checked facts

- Tauri core crate: 2.12.3, released 2026-10-10. No Tauri 3 on the release page.
- Notion API: latest version is 2026-03-11. Since 2025-09-03, tables are
  queried through `POST /v1/data_sources/{data_source_id}/query`, not by
  database ID. A database lists its data sources when retrieved. In 2026-03-11
  the `archived` field is renamed `in_trash`.
- Notion limits: data source queries are capped near 3 requests per second per
  connection on every plan. HTTP 429 responses carry a `Retry-After` header in
  seconds.
- Notion query: cursor pagination (`has_more`, `next_cursor`, `start_cursor`),
  timestamp filters and sorts on `created_time` and `last_edited_time`.
  The database must be shared with the connection, which needs read content
  capability.
- e-means: mini-batch k-means with exponential-moving-average updates. `k` is
  still an input. Code is in the Faiss repository under `contrib/e_means`
  (`Emeans` for PyTorch tensors, `NumpyEmeans` for NumPy; 5 epochs by default).
- X post IDs encode a timestamp. Confirmed on this data: computed days line up
  with the Date column.

### Not yet verified

- Whether Notion's page `created_time` is the true save time for every row. If
  rows were ever bulk-imported into Notion, it would be the import time. Check
  on the first API sync by comparing with the Date column.
- How to read publication dates for GitHub, Hugging Face, YouTube and generic
  pages. Read each platform's current documentation when building the script.
- The capability and endpoint details for writing back to Notion.
- Maximum `page_size` for Notion queries. The code follows the cursor.
- e-means behaviour on a thousand points, whether `NumpyEmeans` is comfortable
  without a GPU, and which Faiss release includes `contrib/e_means`.
- Embedding model, 2D projection, SOM and charting libraries. Each is chosen at
  the start of its phase after reading current docs.

## 4. Data model

Curated data and source data are kept apart. `bookmarks` is what the user owns
and edits. `source_records` is what arrived from outside, kept untouched.

```sql
CREATE TABLE bookmarks (
  id               TEXT PRIMARY KEY,   -- local UUID, not the Notion ID
  url              TEXT NOT NULL,
  url_normalized   TEXT NOT NULL,      -- for duplicate detection
  domain           TEXT NOT NULL,
  platform         TEXT,               -- derived from domain
  title            TEXT,
  summary          TEXT,
  saved_at         TEXT,               -- ISO 8601 UTC
  saved_source     TEXT,               -- notion_created | date_column | manual
  published_at     TEXT,
  published_source TEXT,               -- x_id | platform_api | page_meta | date_column | manual
  created_at       TEXT NOT NULL,      -- row creation in Atlas
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT,               -- soft delete: a tombstone, never re-imported
  merged_into      TEXT REFERENCES bookmarks(id)
);

CREATE TABLE source_records (
  id            INTEGER PRIMARY KEY,
  bookmark_id   TEXT NOT NULL REFERENCES bookmarks(id),
  source        TEXT NOT NULL,         -- notion | csv
  external_id   TEXT,                  -- Notion page ID when known
  fetched_at    TEXT NOT NULL,
  external_edited_at TEXT,
  raw           TEXT NOT NULL,         -- JSON exactly as received
  pending       INTEGER NOT NULL DEFAULT 0,  -- upstream change awaiting review
  UNIQUE (source, external_id)
);

CREATE TABLE tags (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE,
  alias_of INTEGER REFERENCES tags(id) -- merged tags keep their history
);

CREATE TABLE bookmark_tags (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  tag_id      INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (bookmark_id, tag_id)
);

CREATE TABLE sync_state (
  data_source_id   TEXT PRIMARY KEY,
  database_id      TEXT NOT NULL,
  last_synced_time TEXT,               -- high-water mark on last_edited_time
  property_map     TEXT NOT NULL       -- JSON: Notion column -> local field
);

-- Grooming
CREATE TABLE script_runs (
  id          TEXT PRIMARY KEY,
  script      TEXT NOT NULL,
  script_version TEXT NOT NULL,
  params      TEXT NOT NULL,           -- JSON
  started_at  TEXT NOT NULL,
  status      TEXT NOT NULL,           -- previewed | applied | undone | failed
  summary     TEXT                     -- JSON: counts per change type
);

CREATE TABLE changes (
  id            INTEGER PRIMARY KEY,
  script_run_id TEXT REFERENCES script_runs(id),  -- NULL for manual edits
  entity        TEXT NOT NULL,         -- bookmark | tag | bookmark_tag
  entity_id     TEXT NOT NULL,
  field         TEXT,
  old_value     TEXT,
  new_value     TEXT,
  applied_at    TEXT
);

-- Analysis
CREATE TABLE embeddings (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  vector      BLOB NOT NULL,           -- float32, little-endian
  text_hash   TEXT NOT NULL,           -- re-embed only when the text changes
  PRIMARY KEY (bookmark_id, model)
);

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  kind        TEXT NOT NULL,           -- cluster | projection | som | tree
  algorithm   TEXT NOT NULL,           -- e-means | k-means | ...
  params      TEXT NOT NULL,           -- JSON, includes seed and model
  app_version TEXT NOT NULL,
  metrics     TEXT                     -- JSON: loss, timing, stability...
);

CREATE TABLE run_items (
  run_id      TEXT NOT NULL REFERENCES runs(id),
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  cluster     INTEGER,
  x REAL, y REAL,
  PRIMARY KEY (run_id, bookmark_id)
);

CREATE TABLE run_artifacts (
  run_id  TEXT NOT NULL REFERENCES runs(id),
  name    TEXT NOT NULL,               -- centroids | som_weights | tree
  data    BLOB NOT NULL,
  PRIMARY KEY (run_id, name)
);

CREATE TABLE cluster_edits (
  run_id   TEXT NOT NULL REFERENCES runs(id),
  cluster  INTEGER NOT NULL,
  label    TEXT,
  note     TEXT,
  merged_into INTEGER,
  PRIMARY KEY (run_id, cluster)
);

-- Saved explorations
CREATE TABLE views (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  run_ids    TEXT NOT NULL,            -- JSON list
  state      TEXT NOT NULL             -- JSON: filters, active view, zoom, selection
);

CREATE TABLE annotations (
  id          TEXT PRIMARY KEY,
  view_id     TEXT REFERENCES views(id),
  bookmark_id TEXT REFERENCES bookmarks(id),
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL   -- schema_version, snapshot info
);
```

Runs store outputs, not only settings: e-means starts from random points and
works in mini-batches, so the same settings may not reproduce the same
clusters.

## 5. Import rules (Notion and CSV in)

1. A record is matched to a bookmark by Notion page ID, then by normalised URL.
2. No match: a new bookmark is created from the record.
3. Match with a tombstone (deleted or merged locally): the record is stored,
   the bookmark stays deleted. Deleted duplicates do not come back.
4. Match with a live bookmark: the record is stored. Local fields are never
   overwritten. If the upstream content changed since the last fetch, the
   record is marked `pending` and shown in a review list where the user accepts
   or ignores each change.
5. Notion sync is incremental: filter on `last_edited_time` on or after the
   high-water mark, follow the cursor, stay under 3 requests per second, sleep
   on 429, commit the mark only when the whole run succeeds.

## 6. Grooming scripts

A grooming script is a Python module with a fixed contract:

```python
NAME = "dedupe-url"
VERSION = "1"
PARAMS = {...}            # declared parameters, rendered as a form in the UI

def plan(db, params):     # read-only: returns the list of proposed changes
def apply(db, changes):   # writes them inside one transaction
```

How a run works, from the interface or the command line:

1. **Preview**: `plan` runs and the proposed changes are shown as a diff
   (counts, then row by row). Nothing is written.
2. **Select**: the user can untick individual changes.
3. **Apply**: the database file is backed up, then changes are written in one
   transaction and recorded in `changes`.
4. **Undo**: any applied run can be reverted from its journal.

Deletion is always a soft delete. Merging keeps one bookmark, unions the tags,
keeps the earliest `saved_at`, and points the others to it with `merged_into`.

Built-in scripts:

| Script | What it does |
| --- | --- |
| `normalize-urls` | Strips tracking parameters, unifies host variants, fills `domain` |
| `dedupe-url` | Merges bookmarks with the same normalised URL |
| `dedupe-similar` | Proposes merges for same title or near-identical content |
| `derive-platform` | Sets `platform` from the domain |
| `normalize-tags` | Merges tags differing by case, hyphen or spacing |
| `merge-tag-synonyms` | Proposes synonym merges for review, never automatic |
| `split-dates` | Sorts the Date column into `saved_at` or `published_at` |
| `published-from-x-id` | Computes `published_at` for X posts, offline |
| `fetch-published-dates` | Reads publication dates from platforms and page metadata |
| `check-links` | Flags dead or redirected links |

User scripts live in a scripts folder next to the database and appear in the
same panel. They run with the user's own rights, so the app only loads scripts
from that folder, and snapshots never carry scripts.

## 7. Notion out

Optional and explicit. `atlas push` sends curated values (tags, dates,
platform) back to matching Notion rows and can move rows deleted in Atlas to
Notion's trash. It always shows a preview first, applies only on confirmation,
and logs every request. Atlas stays the reference; a failed push changes
nothing locally.

## 8. Phases

Each phase ends with something usable and a short written check.

### Phase 0: scaffolding

- Python package layout, tests, lint, CI.
- Schema migrations with a `schema_version` in `meta`.
- A small synthetic bookmark set for tests, so no private data is in the repo.

### Phase 1: local store and import

- `atlas init`, `atlas import-csv <file>`, `atlas export --format csv|json`.
- `atlas connect` and `atlas sync` for Notion, following the import rules.
- Done when: importing the same CSV twice changes nothing; a second `sync`
  writes nothing; a bookmark deleted locally stays deleted after a sync.

### Phase 2: grooming

- Script contract, preview, journal, backup, undo.
- All built-in scripts except `fetch-published-dates` and `check-links`, which
  need network access and come last.
- Review list for pending upstream changes.
- Done when: the real collection is deduplicated, tags are normalised, both
  dates are filled where possible, and every run can be undone.

### Phase 3: embeddings and the e-means test

- `atlas embed`: one vector per bookmark from title, summary and tags.
- `atlas cluster --algo e-means|k-means --k N --seed S`: writes a run.
- `atlas compare`, for the same `k` and several seeds: final loss, time,
  stability across seeds, agreement between algorithms, cluster sizes, and a
  readable sample (top tags and five titles per cluster).
- Sweep `k` to feed a "suggested k" in the UI.
- Done when: a short report in `docs/` states whether e-means stays the
  default at this collection size, with numbers.

### Phase 4: desktop shell, table, map, grooming panel

- Tauri app driving the Python package.
- Table with shared filters: tags, both date ranges, domain, cluster, search.
- Inline editing and soft delete, recorded in the journal.
- Grooming panel: script list, parameter form, preview diff, apply, undo,
  history.
- Map view: 2D projection coloured by cluster; hover for summary, click to
  open; lasso selection feeds the table.
- Editable cluster labels generated from top tags.

### Phase 5: saved views, snapshots, Notion out

- Save view: filters, active view, zoom, selection, notes.
- Export snapshot: one SQLite file with the bookmarks in scope, the runs and
  the view. Options: whole collection or current filter; with or without
  embeddings; source records and private notes stripped by default.
- Open snapshot read-only, with "fork into my workspace".
- Compare two snapshots.
- Optional single-file HTML export of the map.
- `atlas push` with preview.

### Phase 6: SOM and pearl tree

- Self-organising map grid: density, dominant tags, neighbouring themes.
- Pearl tree: hierarchical clusters as nested collections; drag, rename, merge
  and split; edits stored per run.

### Phase 7: links and tendencies

- Link graph: shared tags, same domain, nearest neighbours.
- Bridges and outliers surfaced as lists.
- Tendencies on both dates: when things were saved, when they were published.
- Reaction time (gap between publication and saving) and rediscoveries.
- Resurfacing of old bookmarks close to recent ones.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| A grooming script damages the collection | Preview, backup before apply, journal, undo, soft delete only |
| Imports undo local curation | Source records kept apart; tombstones; pending review |
| A user script does something unsafe | Loaded only from the user's own folder; never shipped in snapshots |
| e-means brings nothing at this scale | Phase 3 measures it; k-means stays as fallback |
| Packaging Python inside a desktop app is heavy | Narrow interface between shell and package; revisit after phase 4 |
| Notion API changes again | Version header pinned; all Notion code in one module |
| Private data leaks through git or snapshots | `.gitignore` covers data files; snapshot export strips by default |

## 10. Open questions

1. Who receives snapshots: people with the app, or people who only look?
2. Embeddings computed locally or through an API?
3. Which operating systems must the app run on?
4. Should `atlas push` ever create rows in Notion, or only update existing ones?

## 11. Sources

- e-means paper: https://arxiv.org/abs/2611.09501
- e-means project page: https://e-means.github.io/
- Notion, query a data source: https://developers.notion.com/reference/query-a-data-source
- Notion, request limits: https://developers.notion.com/reference/request-limits
- Notion, upgrade guide 2025-09-03: https://developers.notion.com/docs/upgrade-guide-2025-09-03
- Notion, upgrade guide 2026-03-11: https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11
- Tauri releases: https://v2.tauri.app/release/
