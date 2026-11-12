"""Tag the bookmarks whose link no longer answers."""

from atlas.groom import Change
from atlas.groom.common import label, tags_of

NAME = "flag-dead-links"
TITLE = "Tag dead links"
DESCRIPTION = """
Uses the answers gathered by "Check the links" below. A bookmark whose page
is gone (the site says "not found", or the site itself no longer exists) gets
a tag, so you can filter on it and decide what to do. Nothing is deleted.
Links that merely refused or timed out are left alone.
"""
VERSION = "1"
PARAMS = [{"name": "tag", "type": "text", "default": "dead link", "label": "Tag to add"}]


def plan(conn, params):
    tag = " ".join(params["tag"].split()) or "dead link"
    changes = []
    for bookmark in conn.execute(
        "SELECT b.*, c.detail FROM bookmarks b JOIN link_checks c ON c.bookmark_id = b.id "
        "AND c.url = b.url WHERE b.deleted_at IS NULL AND c.result = 'gone' "
        "ORDER BY b.created_at, b.id"
    ).fetchall():
        if tag in tags_of(conn, bookmark["id"]):
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
                note=f"add tag “{tag}”: {bookmark['detail']}",
            )
        )
    return changes
