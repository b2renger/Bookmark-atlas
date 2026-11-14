-- What a language model, and a fresh reading of the pages, add to the
-- collection. Nothing here changes a bookmark: the grooming scripts
-- "summary-from-model" and "tags-from-model" read these tables and propose
-- changes that are previewed, applied and undone like any other.

-- The readable text of a bookmark's page, as fetched.
CREATE TABLE page_texts (
    bookmark_id TEXT PRIMARY KEY REFERENCES bookmarks(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- ok | none | error | skipped
    title       TEXT,
    description TEXT,               -- the page's own description of itself
    text        TEXT,               -- main text, trimmed
    detail      TEXT                -- why there is no text, when so
);

-- What a model wrote about one bookmark: a summary, or a list of tags.
CREATE TABLE llm_notes (
    bookmark_id TEXT NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,      -- summary | tags
    value       TEXT NOT NULL,      -- text, or a JSON list of tag names
    model       TEXT NOT NULL,
    based_on    TEXT NOT NULL,      -- hash of what the model was given
    created_at  TEXT NOT NULL,
    PRIMARY KEY (bookmark_id, kind)
);

-- Answers to questions asked of a set of bookmarks, kept to be read again.
CREATE TABLE llm_answers (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- common | briefing | gaps | path | question | collection
    question     TEXT,
    bookmark_ids TEXT NOT NULL,     -- JSON list, in the order they were numbered
    answer       TEXT NOT NULL,
    model        TEXT NOT NULL
);
