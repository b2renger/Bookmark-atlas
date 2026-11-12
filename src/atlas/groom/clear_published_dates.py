"""Take back publication dates that came from one source."""

from atlas.groom import Change
from atlas.groom.common import label, live_bookmarks

NAME = "clear-published-dates"
TITLE = "Remove publication dates from one source"
DESCRIPTION = """
Empties the publication date of every bookmark whose date came from the
source you choose, for when that source turns out to be unreliable. The dates
taken from the Date column of the import are the ones nothing has verified.
Like every script, the run can be undone.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "source",
        "type": "choice",
        "default": "date_column",
        "label": "Remove the dates that came from",
        "choices": [
            {"value": "date_column", "label": "the Date column of the import (not verified)"},
            {"value": "page", "label": "the bookmarked page"},
            {"value": "github", "label": "GitHub (repository creation day)"},
            {"value": "x_id", "label": "the X post ID"},
        ],
    }
]


def plan(conn, params):
    changes = []
    for bookmark in live_bookmarks(conn):
        if bookmark["published_at"] is None or bookmark["published_source"] != params["source"]:
            continue
        group = f"bookmark:{bookmark['id']}"
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                "published_at",
                bookmark["published_at"],
                None,
                group=group,
                title=label(bookmark),
                note=f"remove the publication date {bookmark['published_at'][:10]}",
            )
        )
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                "published_source",
                bookmark["published_source"],
                None,
                group=group,
                title=label(bookmark),
                note="and its source",
            )
        )
    return changes
