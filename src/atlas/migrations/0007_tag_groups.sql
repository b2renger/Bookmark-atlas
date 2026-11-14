-- Tags a language model judged to say the same thing (a synonym, a
-- translation, an abbreviation). Nothing is merged from here: the grooming
-- script "merge-tags-from-model" reads this table and proposes the merges.
CREATE TABLE llm_tag_groups (
    tag        TEXT PRIMARY KEY,    -- a tag's name, lower-cased
    family     TEXT NOT NULL,       -- the same value for every tag of one group
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
