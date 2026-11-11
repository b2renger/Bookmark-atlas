-- Analysis: text vectors, and runs (clusterings, projections) with their results.
-- Runs store outputs, not only settings: clustering starts from random points,
-- so the same settings do not always reproduce the same result.

CREATE TABLE embeddings (
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  model       TEXT NOT NULL,          -- how the vectors were made, e.g. lsa-128
  dim         INTEGER NOT NULL,
  vector      BLOB NOT NULL,          -- float32, little-endian
  text_hash   TEXT NOT NULL,          -- hash of the text the vector was made from
  PRIMARY KEY (bookmark_id, model)
);

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- cluster | projection | som | tree
  algorithm   TEXT NOT NULL,          -- e-means | k-means | ...
  params      TEXT NOT NULL,          -- JSON, includes seed and vector model
  app_version TEXT NOT NULL,
  metrics     TEXT,                   -- JSON: loss, timing, ...
  name        TEXT                    -- optional label given by the user
);

CREATE TABLE run_items (
  run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  cluster     INTEGER,
  x REAL, y REAL,
  PRIMARY KEY (run_id, bookmark_id)
);

CREATE INDEX run_items_cluster ON run_items(run_id, cluster);

CREATE TABLE run_artifacts (
  run_id  TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name    TEXT NOT NULL,              -- centroids | som_weights | tree
  data    BLOB NOT NULL,
  PRIMARY KEY (run_id, name)
);

CREATE TABLE cluster_edits (
  run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cluster     INTEGER NOT NULL,
  label       TEXT,
  note        TEXT,
  merged_into INTEGER,
  PRIMARY KEY (run_id, cluster)
);
