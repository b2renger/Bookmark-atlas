# Grooming scripts

Grooming cleans the collection: duplicates, tag spellings, dates, platforms.
It is done by scripts that run from the **Grooming** tab or from the command
line (`atlas groom ...`).

## How a run works

1. **Preview.** The script reads the database and proposes changes. Nothing is
   written. Changes are shown in groups.
2. **Select.** Untick the groups you do not want.
3. **Apply.** The database is copied to `backups/` first. Then the accepted
   groups are written together: all of them, or none if anything goes wrong.
   Every change is recorded in a journal.
4. **Undo.** Any run in the history can be reversed.

Two safety rules are enforced by the app, not by the scripts:

- A script cannot write. If it modifies the database while planning, the run
  is refused.
- Each change says what value it expects to replace. If the value is no longer
  that one (you edited it, or another run changed it), the whole run is
  refused and nothing is written. The same holds for undo.

## Built-in scripts

| Name | What it does |
| --- | --- |
| `merge-duplicates` | Folds rows imported for a link you already had into the bookmark, and merges bookmarks whose links point to the same page. The kept bookmark gains the tags and keeps the earliest save date. |
| `normalize-tags` | Merges tags that differ only by capitals, hyphens, dots or spacing. The most used spelling wins. The others become aliases, so later imports map to the kept spelling. |
| `derive-platform` | Sets the platform (X, GitHub, Hugging Face, YouTube, ...) from the link's site. |
| `published-from-x-id` | Computes the publication time of X posts from the ID in their link. Exact, and offline. |
| `split-dates` | Stores a Date without a time of day as the publication date (default) or the save date. |

The order above is a good order to run them in.

## Writing your own

Put a `.py` file in the `scripts` folder beside your database (**Atlas > Open
data folder** in the app). It appears in the Grooming tab next time you open
it. Files starting with `_` are ignored.

A script is a Python file with a name and a `plan` function:

```python
from atlas.groom import Change

NAME = "tag-github-repos"  # unique, used on the command line
TITLE = "Tag GitHub repositories"  # shown in the interface
DESCRIPTION = "Adds the tag “Repository” to every GitHub bookmark."
PARAMS = []  # optional, see below


def plan(conn, params):
    """Read the database, return the changes you propose. Do not write."""
    changes = []
    for row in conn.execute(
        "SELECT id, title FROM bookmarks WHERE deleted_at IS NULL AND domain = 'github.com'"
    ):
        has_tag = conn.execute(
            "SELECT 1 FROM bookmark_tags JOIN tags ON tags.id = tag_id "
            "WHERE bookmark_id = ? AND name = 'Repository'",
            (row["id"],),
        ).fetchone()
        if not has_tag:
            changes.append(
                Change(
                    "bookmark_tag",
                    row["id"],
                    "tag",
                    None,
                    "Repository",
                    group=row["id"],
                    title=row["title"],
                )
            )
    return changes
```

`conn` is a SQLite connection (rows can be read by column name). The tables
are described in `docs/IMPLEMENTATION_PLAN.md`, section 4.

### The changes a script may propose

`Change(entity, entity_id, field, old, new, group=..., title=..., note=...)`

| entity | entity_id | field | old → new |
| --- | --- | --- | --- |
| `bookmark` | bookmark ID | `title`, `summary`, `platform`, `saved_at`, `saved_source`, `published_at`, `published_source`, `url_normalized`, `domain`, `deleted_at`, `merged_into` | current value → new value |
| `bookmark_tag` | bookmark ID | `tag` | `None` → tag name (add), or tag name → `None` (remove) |
| `tag` | tag name | `alias_of` | `None` → name of the tag it merges into |
| `source_record` | record ID | `pending` | `"1"` → `"0"` |

- `old` must be the value currently in the database. That is what makes a run
  safe to apply and possible to undo.
- `group` ties together changes that only make sense together. The user
  accepts or rejects whole groups. Use the bookmark ID for per-bookmark
  changes.
- `title` names the group in the preview; `note` replaces the default
  description of the change.
- To delete a bookmark, set `deleted_at` from `None` to `atlas.groom.NOW`.
  Nothing is ever removed from the database.
- A script that finds nothing to do returns an empty list. Running a script a
  second time should find nothing: only propose a change when the value
  differs.

### Parameters

```python
PARAMS = [
    {"name": "overwrite", "type": "bool", "default": False, "label": "Replace existing values"},
    {
        "name": "mode",
        "type": "choice",
        "default": "a",
        "label": "Mode",
        "choices": [{"value": "a", "label": "First way"}, {"value": "b", "label": "Second way"}],
    },
    {"name": "prefix", "type": "text", "default": "", "label": "Prefix"},
]
```

They are shown as a form and arrive in `params` as a dictionary.

### A word of caution

A script is a program that runs on your computer with your rights. The app
loads scripts only from your own `scripts` folder and never ships them inside
a snapshot. Read a script before dropping it in, the same way you would for
any program someone sends you.
