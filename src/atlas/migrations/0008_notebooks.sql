-- Notebooks: a set of sources, a conversation about them, and notes kept.
-- Pages are cut into passages so an answer can point at the words it rests on.

CREATE TABLE passages (
    id          INTEGER PRIMARY KEY,
    bookmark_id TEXT NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,   -- 0 is the bookmark's own card (title, summary, tags)
    text        TEXT NOT NULL,
    basis       TEXT NOT NULL,      -- hash of what the bookmark's passages were cut from
    UNIQUE (bookmark_id, ord)
);

CREATE TABLE passage_vectors (
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    PRIMARY KEY (passage_id, model)
);

CREATE TABLE notebooks (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE notebook_sources (
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    bookmark_id TEXT NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    used        INTEGER NOT NULL DEFAULT 1,   -- unticked sources stay listed but are not read
    PRIMARY KEY (notebook_id, bookmark_id)
);

CREATE TABLE notebook_messages (
    id          INTEGER PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,      -- user | assistant
    content     TEXT NOT NULL,      -- the question, or the answer as JSON (see notebook.py)
    model       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE notebook_notes (
    id          TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,      -- an answer as JSON, or plain text the person wrote
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
