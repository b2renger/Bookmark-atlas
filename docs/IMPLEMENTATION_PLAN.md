# Implementation plan

Written 2026-11-11. Facts marked "checked" were read at the source on that date;
re-check them before relying on them.

## 1. Goals

1. Import bookmarks from a Notion table into a local, universal format, and
   keep the local copy up to date by fetching only what is missing or changed.
2. Explore the collection through clustering and several linked visualisations
   that encourage discovery, logical links and trend spotting.
3. Use e-means as the default clustering engine and measure it against classic
   k-means on real bookmark data.
4. Save explorations (results and view configuration) and share them as a
   single file.

Not goals for now: capturing new bookmarks, generating summaries or tags (an
existing tool does this), mobile, multi-user sync.

## 2. Architecture

```
Notion API ──> extractor ──> SQLite file <──> analysis sidecar (Python)
                                 ^
                                 |
                          desktop shell (Tauri) ──> web frontend (views)
                                 |
                                 └──> snapshot file / HTML export
```

| Part | Choice | Why |
| --- | --- | --- |
| Desktop shell | Tauri 2 | Small binaries, web frontend for visualisation |
| Frontend | TypeScript, framework to be chosen in phase 3 | Views are web-native |
| Data and analysis | Python sidecar | e-means ships as Python in Faiss |
| Storage | SQLite, one file | Readable from nearly any tool or language |
| Secrets | OS keychain | The Notion token never touches the data file |

The extractor and analysis are a Python package with a command line, usable
without the desktop shell. Phases 1 and 2 need no UI at all.

### Checked facts

- Tauri core crate: 2.12.3, released 2026-10-10. No Tauri 3 on the release page.
- Notion API: latest version is 2026-03-11. Since 2025-09-03, tables are
  queried through `POST /v1/data_sources/{data_source_id}/query`, not by
  database ID. A database lists its data sources when retrieved.
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

### Not yet verified

- Maximum `page_size` for Notion queries. The code follows the cursor and does
  not depend on it.
- e-means behaviour on small collections (thousands of points). The paper
  sections read so far use a million vectors or more. Phase 2 measures this.
- Whether `NumpyEmeans` is comfortable without a GPU, and which Faiss release
  includes `contrib/e_means`.
- Embedding model, 2D projection library, SOM library and charting libraries.
  Each is chosen at the start of its phase after reading current docs.

## 3. Data model

Main database, also the snapshot format.

```sql
CREATE TABLE bookmarks (
  id              TEXT PRIMARY KEY,   -- Notion page ID
  url             TEXT,
  title           TEXT,
  summary         TEXT,
  created_time    TEXT NOT NULL,      -- ISO 8601, from Notion
  last_edited_time TEXT NOT NULL,
  archived        INTEGER NOT NULL DEFAULT 0,
  raw_json        TEXT NOT NULL       -- full Notion page object
);

CREATE TABLE tags (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE
);

CREATE TABLE bookmark_tags (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  tag_id      INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (bookmark_id, tag_id)
);

CREATE TABLE sync_state (
  data_source_id   TEXT PRIMARY KEY,
  database_id      TEXT NOT NULL,
  last_synced_time TEXT,              -- high-water mark on last_edited_time
  property_map     TEXT NOT NULL      -- JSON: Notion column -> local field
);

CREATE TABLE embeddings (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  vector      BLOB NOT NULL,          -- float32, little-endian
  text_hash   TEXT NOT NULL,          -- re-embed only when the text changes
  PRIMARY KEY (bookmark_id, model)
);

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- cluster | projection | som | tree
  algorithm   TEXT NOT NULL,          -- e-means | k-means | ...
  params      TEXT NOT NULL,          -- JSON, includes seed and model
  app_version TEXT NOT NULL,
  metrics     TEXT                    -- JSON: loss, timing, silhouette...
);

CREATE TABLE run_items (
  run_id      TEXT NOT NULL REFERENCES runs(id),
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  cluster     INTEGER,
  x REAL, y REAL,                     -- map or SOM cell coordinates
  PRIMARY KEY (run_id, bookmark_id)
);

CREATE TABLE run_artifacts (
  run_id  TEXT NOT NULL REFERENCES runs(id),
  name    TEXT NOT NULL,              -- centroids | som_weights | tree
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

CREATE TABLE views (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  run_ids    TEXT NOT NULL,           -- JSON list
  state      TEXT NOT NULL            -- JSON: filters, active view, zoom, selection
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

Runs store outputs, not only settings. e-means starts from random points and
works in mini-batches, so the same settings may not reproduce the same
clusters.

## 4. Phases

Each phase ends with something usable and a short written check.

### Phase 0: scaffolding

- Python package layout, tests, lint, CI.
- Schema migrations with a `schema_version` in `meta`.
- A small synthetic bookmark set for tests, so no private data is in the repo.

### Phase 1: Notion extractor and local store

Commands:

- `atlas connect`: store the token in the keychain, resolve the database to its
  data source, read the columns and write `property_map`.
- `atlas sync`: incremental import.
- `atlas export --format csv|json`.

Sync algorithm:

1. Read `last_synced_time`. If empty, run a full import.
2. Query the data source filtered on `last_edited_time` on or after the mark,
   sorted ascending.
3. Follow the cursor until `has_more` is false. Stay under 3 requests per
   second. On 429, sleep for `Retry-After` then resume.
4. Upsert each page by ID inside one transaction; update tags.
5. Commit the new mark only if the whole run succeeded. The mark is the highest
   `last_edited_time` seen, so an interrupted run is simply repeated.
6. Optional `--full` pass to detect pages removed or archived in Notion.

Done when: a second `sync` right after the first makes one request and writes
nothing; editing one row in Notion updates exactly one local row.

Open point: if summaries live in the page body, not in a column, each new page
needs extra block requests. Decide after looking at the real table.

### Phase 2: embeddings and the e-means test

- `atlas embed`: build one vector per bookmark from title, summary and tags.
  Skips rows whose `text_hash` is unchanged.
- `atlas cluster --algo e-means|k-means --k N --seed S`: writes a run.
- `atlas compare`: report for the same `k` and several seeds:
  - final loss (mean squared error) and wall-clock time,
  - stability across seeds (adjusted Rand index between runs),
  - agreement between the two algorithms,
  - cluster size distribution and empty clusters,
  - a readable sample: top tags and five titles per cluster.
- Sweep `k` and record the curve, to feed a "suggested k" in the UI.

Clustering sits behind one interface (`fit(vectors, k, seed) -> labels,
centroids, metrics`) so engines can be swapped.

Done when: a short report in `docs/` states whether e-means is worth keeping as
default at this collection size, with numbers.

### Phase 3: desktop shell, table and map

- Tauri app that starts the Python sidecar and talks to it over a local
  channel.
- Sync button with progress and last sync time.
- Table view with filters: tags, date range, domain, cluster, text search.
- Map view: 2D projection coloured by cluster; hover for summary, click to
  open; lasso selection feeds the table.
- Filters are shared across all views.
- Cluster labels generated from top tags, editable.

### Phase 4: saved views and snapshots

- Save view: name, filters, active view, zoom, selection, notes.
- Export snapshot: one SQLite file with the same schema, holding the bookmarks
  in scope, the runs used and the view. Options: whole collection or current
  filter; include embeddings or not; strip `raw_json` and private notes.
- Open snapshot: read-only mode, with "fork into my workspace".
- Compare two snapshots: clusters that grew, shrank, appeared.
- Optional: single-file HTML export of the map for people without the app.

Done when: a snapshot exported on one machine opens on another and shows the
same map, clusters, labels and notes.

### Phase 5: SOM and pearl tree

- Self-organising map grid: cell density, dominant tags, neighbouring themes.
- Pearl tree: hierarchical clustering shown as nested collections; drag, rename,
  merge and split; edits stored per run.

### Phase 6: links, tendencies, refinement

- Link graph: edges from shared tags, same domain, nearest neighbours.
- Bridges (items between two clusters) and outliers surfaced as lists.
- Tendencies: cluster and tag volume over time; growing and fading topics.
- Resurfacing: old bookmarks close to recent ones.
- Tag hygiene: suggested merges for near-duplicate tags.
- Optional write-back of refined tags to Notion (needs update capability and an
  explicit confirmation step).

## 5. Risks

| Risk | Mitigation |
| --- | --- |
| e-means brings nothing at small scale | Phase 2 measures it; k-means stays as fallback |
| Packaging Python inside a desktop app is heavy | Keep the sidecar interface narrow; revisit after phase 3 |
| Notion API changes again | Pin the version header; all Notion code lives in one module |
| Private data leaks through git or snapshots | `.gitignore` covers data files; snapshot export has strip options |
| Clusters are hard to read | Auto-labels, sample titles and manual renaming from phase 3 |

## 6. Open questions

1. How many bookmarks are in the table today?
2. Are summary and tags stored as columns or in the page body?
3. Who receives snapshots: people with the app, or people who only look?
4. Embeddings computed locally or through an API?
5. Which operating systems must the app run on?

## 7. Sources

- e-means paper: https://arxiv.org/abs/2611.09501
- e-means project page: https://e-means.github.io/
- Notion, query a data source: https://developers.notion.com/reference/query-a-data-source
- Notion, request limits: https://developers.notion.com/reference/request-limits
- Notion, upgrade guide 2025-09-03: https://developers.notion.com/docs/upgrade-guide-2025-09-03
- Notion, upgrade guide 2026-03-11: https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11
- Tauri releases: https://v2.tauri.app/release/
