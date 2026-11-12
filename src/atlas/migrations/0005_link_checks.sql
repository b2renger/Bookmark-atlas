-- Whether each bookmark's link still answers, as last checked. The grooming
-- script "flag-dead-links" reads this table and never goes online itself.
CREATE TABLE link_checks (
    bookmark_id TEXT PRIMARY KEY REFERENCES bookmarks(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    result      TEXT NOT NULL,       -- ok | gone | unsure | skipped
    detail      TEXT
);
