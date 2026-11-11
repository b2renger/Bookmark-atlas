-- Saved explorations: a named state of the interface, with optional notes.

CREATE TABLE views (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL,
  state      TEXT NOT NULL            -- JSON: tab, search, filters, selection, map position
);

CREATE TABLE annotations (
  id          TEXT PRIMARY KEY,
  view_id     TEXT REFERENCES views(id) ON DELETE CASCADE,
  bookmark_id TEXT REFERENCES bookmarks(id),
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
