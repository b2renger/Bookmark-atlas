"""Add the tags a language model picked for each page."""

import json

from atlas.groom import Change
from atlas.groom.common import label, tags_of

NAME = "tags-from-model"
TITLE = "Tags picked by the language model"
DESCRIPTION = """
Uses the tags picked by "Write summaries and tags" above. This script does not
go online and does not call the model. It only adds tags, never removes one.
By default it adds only tags the collection already uses, so the model cannot
grow the list of tags; choose "new tags too" to let it.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "which",
        "label": "Which tags",
        "type": "choice",
        "choices": [
            {"value": "known", "label": "tags already in use"},
            {"value": "new", "label": "new tags too"},
        ],
        "default": "known",
    }
]


def plan(conn, params):
    allow_new = params.get("which") == "new"
    known = {row[0].casefold(): row[0] for row in conn.execute("SELECT name FROM tags")}
    changes = []
    rows = conn.execute(
        "SELECT b.*, n.value AS picked FROM bookmarks b "
        "JOIN llm_notes n ON n.bookmark_id = b.id AND n.kind = 'tags' "
        "WHERE b.deleted_at IS NULL ORDER BY b.created_at, b.id"
    ).fetchall()
    for bookmark in rows:
        try:
            picked = json.loads(bookmark["picked"])
        except ValueError:
            continue
        have = {name.casefold() for name in tags_of(conn, bookmark["id"])}
        for tag in picked if isinstance(picked, list) else []:
            if not isinstance(tag, str) or tag.casefold() in have:
                continue
            if tag.casefold() not in known and not allow_new:
                continue
            name = known.get(tag.casefold(), tag)
            have.add(tag.casefold())
            changes.append(
                Change(
                    "bookmark_tag",
                    bookmark["id"],
                    "tag",
                    None,
                    name,
                    group=f"bookmark:{bookmark['id']}",
                    title=label(bookmark),
                    note=f"add “{name}”" + ("" if tag.casefold() in known else " (a new tag)"),
                )
            )
    return changes
