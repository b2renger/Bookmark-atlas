"""Fill publication dates from what was read on the bookmarked pages."""

from atlas.groom import Change
from atlas.groom.common import label

NAME = "published-from-page"
TITLE = "Publication date read from the page"
DESCRIPTION = """
Uses the dates found by "Look up publication dates" above: the date a page
declares for itself, or the day a GitHub repository was created. This script
does not go online; run the lookup first. It only fills bookmarks whose
publication date is empty.
"""
VERSION = "1"
PARAMS = []


def plan(conn, params):
    changes = []
    rows = conn.execute(
        "SELECT b.*, f.published_at AS found_at, f.found_in FROM bookmarks b "
        "JOIN page_facts f ON f.bookmark_id = b.id "
        "WHERE b.deleted_at IS NULL AND b.published_at IS NULL AND f.status = 'found' "
        "AND f.published_at IS NOT NULL AND f.url = b.url ORDER BY b.created_at, b.id"
    ).fetchall()
    for bookmark in rows:
        source = "github" if bookmark["found_in"] == "github" else "page"
        where = (
            "the day the repository was created, from GitHub"
            if source == "github"
            else f"read from the page ({bookmark['found_in']})"
        )
        group = f"bookmark:{bookmark['id']}"
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                "published_at",
                None,
                bookmark["found_at"],
                group=group,
                title=label(bookmark),
                note=f"published {bookmark['found_at'][:10]}, {where}",
            )
        )
        if bookmark["published_source"] != source:
            changes.append(
                Change(
                    "bookmark",
                    bookmark["id"],
                    "published_source",
                    bookmark["published_source"],
                    source,
                    group=group,
                    title=label(bookmark),
                    note=f"source: {source}",
                )
            )
    return changes
