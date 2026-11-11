"""Decide what a Date without a time of day means, and store it."""

from atlas.dates import parse_date
from atlas.groom import Change
from atlas.groom.common import label, live_bookmarks, record_fields
from atlas.urls import x_status_id

NAME = "split-dates"
TITLE = "Sort out dates without a time"
DESCRIPTION = """
Some rows carry a Date with no time of day. On X posts that date turned out to
be the day the post was published, not the day it was saved, so the import
left those rows without any date. This script stores such a date as the
publication date (or as the save date, if you choose so).

X posts are skipped: "Publication date of X posts" gives them an exact time.
Only bookmarks whose target field is still empty are touched.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "meaning",
        "type": "choice",
        "default": "published",
        "label": "A date without a time is the day the content was",
        "choices": [
            {"value": "published", "label": "published (what the X posts show)"},
            {"value": "saved", "label": "saved"},
        ],
    }
]


def date_only_value(conn, bookmark_id: str) -> str | None:
    """The first date-only Date among a bookmark's source records, oldest first."""
    for record in conn.execute(
        "SELECT * FROM source_records WHERE bookmark_id = ? ORDER BY id", (bookmark_id,)
    ):
        parsed = parse_date(record_fields(conn, record)["date_text"])
        if parsed is not None and parsed.date_only:
            return parsed.iso
    return None


def plan(conn, params):
    field = "published_at" if params["meaning"] == "published" else "saved_at"
    source_field = field.replace("_at", "_source")
    changes = []
    for bookmark in live_bookmarks(conn):
        if bookmark[field] is not None or x_status_id(bookmark["url"]):
            continue
        day = date_only_value(conn, bookmark["id"])
        if day is None:
            continue
        group = f"bookmark:{bookmark['id']}"
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                field,
                None,
                day,
                group=group,
                title=label(bookmark),
                note=f"{params['meaning']} on {day} ({bookmark['domain']})",
            )
        )
        if bookmark[source_field] != "date_column":
            changes.append(
                Change(
                    "bookmark",
                    bookmark["id"],
                    source_field,
                    bookmark[source_field],
                    "date_column",
                    group=group,
                    title=label(bookmark),
                    note="source: Date column",
                )
            )
    return changes
