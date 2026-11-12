-- What was read from a bookmark's own page when looking for its publication
-- date. One row per bookmark looked up; the grooming script
-- "published-from-page" reads this table and never goes online itself.
CREATE TABLE page_facts (
    bookmark_id  TEXT PRIMARY KEY REFERENCES bookmarks(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,      -- found | none | error | skipped
    published_at TEXT,               -- ISO date or date-time, when found
    found_in     TEXT,               -- where on the page it was read
    detail       TEXT                -- why nothing was found, when so
);
