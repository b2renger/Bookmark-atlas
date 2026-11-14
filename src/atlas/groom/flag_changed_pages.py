"""Tag the bookmarks whose page is no longer what its summary describes."""

import json

from atlas.groom import Change
from atlas.groom.common import label, tags_of

NAME = "flag-changed-pages"
TITLE = "Flag pages that changed since they were saved"
DESCRIPTION = """
Uses "Compare then and now" above, where the language model read each page as
it is today beside the summary written when it was saved. Bookmarks whose page
moved on get the tag "page changed"; those whose page is now something else (a
parked domain, a shop, another subject) get "page replaced". The reason is
shown for each. Only tags are added: filter on them to review, rewrite or
remove these bookmarks. This script does not go online.
"""
VERSION = "1"
PARAMS = []
TAGS = {"changed": "page changed", "replaced": "page replaced"}


def plan(conn, params):
    changes = []
    rows = conn.execute(
        "SELECT b.*, n.value AS judged FROM bookmarks b "
        "JOIN llm_notes n ON n.bookmark_id = b.id AND n.kind = 'then_now' "
        "JOIN page_texts p ON p.bookmark_id = b.id AND p.url = b.url "
        "WHERE b.deleted_at IS NULL ORDER BY b.created_at, b.id"
    ).fetchall()
    for bookmark in rows:
        judged = json.loads(bookmark["judged"])
        tag = TAGS.get(judged.get("verdict"))
        if not tag or tag in tags_of(conn, bookmark["id"]):
            continue
        changes.append(
            Change(
                "bookmark_tag",
                bookmark["id"],
                "tag",
                None,
                tag,
                group=f"bookmark:{bookmark['id']}",
                title=label(bookmark),
                note=f"add “{tag}”: {judged.get('note') or 'no reason given'}",
            )
        )
    return changes
