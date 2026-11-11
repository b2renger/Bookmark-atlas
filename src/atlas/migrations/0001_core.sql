-- Core tables: curated bookmarks, untouched source records, tags, sync state
-- and the grooming journal. Analysis and saved-view tables come with their
-- own migrations in later phases.

CREATE TABLE bookmarks (
  id               TEXT PRIMARY KEY,   -- local UUID, not the Notion ID
  url              TEXT NOT NULL,
  url_normalized   TEXT NOT NULL,      -- for matching and duplicate detection
  domain           TEXT NOT NULL,
  platform         TEXT,               -- derived from domain by a grooming script
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

CREATE INDEX bookmarks_url_normalized ON bookmarks(url_normalized);
CREATE INDEX bookmarks_domain ON bookmarks(domain);

CREATE TABLE source_records (
  id                 INTEGER PRIMARY KEY,
  bookmark_id        TEXT NOT NULL REFERENCES bookmarks(id),
  source             TEXT NOT NULL,    -- notion | csv
  external_id        TEXT,             -- Notion page ID when known
  fetched_at         TEXT NOT NULL,
  external_edited_at TEXT,
  raw                TEXT NOT NULL,    -- JSON exactly as received
  raw_hash           TEXT NOT NULL,    -- detects an unchanged record on re-import
  pending            INTEGER NOT NULL DEFAULT 0,  -- upstream content awaiting review
  UNIQUE (source, external_id)
);

CREATE INDEX source_records_bookmark ON source_records(bookmark_id);
CREATE INDEX source_records_hash ON source_records(source, raw_hash);

CREATE TABLE tags (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE,
  alias_of INTEGER REFERENCES tags(id) -- merged tags keep their history
);

CREATE TABLE bookmark_tags (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  tag_id      INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (bookmark_id, tag_id)
);

CREATE INDEX bookmark_tags_tag ON bookmark_tags(tag_id);

CREATE TABLE sync_state (
  data_source_id   TEXT PRIMARY KEY,
  database_id      TEXT NOT NULL,
  last_synced_time TEXT,               -- high-water mark on last_edited_time
  property_map     TEXT NOT NULL       -- JSON: Notion column -> local field
);

CREATE TABLE script_runs (
  id             TEXT PRIMARY KEY,
  script         TEXT NOT NULL,
  script_version TEXT NOT NULL,
  params         TEXT NOT NULL,        -- JSON
  started_at     TEXT NOT NULL,
  status         TEXT NOT NULL,        -- previewed | applied | undone | failed
  summary        TEXT                  -- JSON: counts per change type
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

CREATE INDEX changes_run ON changes(script_run_id);

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
