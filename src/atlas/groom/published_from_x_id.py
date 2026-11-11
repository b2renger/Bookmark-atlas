"""Compute when an X post was published from the ID in its link."""

from datetime import UTC, datetime, timedelta

from atlas.groom import Change
from atlas.groom.common import label, live_bookmarks
from atlas.urls import x_status_id

NAME = "published-from-x-id"
TITLE = "Publication date of X posts"
DESCRIPTION = """
The number in an X post link encodes the moment the post was created. This
script reads it and fills the publication date, exactly and without going
online. It only touches bookmarks whose publication date is empty, unless
told otherwise.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "overwrite",
        "type": "bool",
        "default": False,
        "label": "Also replace publication dates that are already set",
    }
]

# Post IDs carry a millisecond timestamp counted from this instant.
X_EPOCH_MS = 1288834974657
# Older posts have small sequential IDs that decode to the first seconds of
# the scheme; a day of margin keeps them out.
FIRST = datetime(2010, 11, 5, tzinfo=UTC)


def published_at(status_id: str) -> datetime | None:
    """Creation time encoded in a post ID. None for IDs older than the scheme."""
    moment = datetime.fromtimestamp(((int(status_id) >> 22) + X_EPOCH_MS) / 1000, tz=UTC)
    if moment < FIRST or moment > datetime.now(UTC) + timedelta(days=1):
        return None
    return moment


def plan(conn, params):
    changes = []
    for bookmark in live_bookmarks(conn):
        status_id = x_status_id(bookmark["url"])
        if not status_id:
            continue
        if bookmark["published_at"] is not None and not params["overwrite"]:
            continue
        moment = published_at(status_id)
        if moment is None:
            continue
        iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        if bookmark["published_at"] == iso and bookmark["published_source"] == "x_id":
            continue
        group = f"bookmark:{bookmark['id']}"
        if bookmark["published_at"] != iso:
            changes.append(
                Change(
                    "bookmark",
                    bookmark["id"],
                    "published_at",
                    bookmark["published_at"],
                    iso,
                    group=group,
                    title=label(bookmark),
                    note=f"published {moment:%Y-%m-%d %H:%M} UTC, read from the post ID",
                )
            )
        if bookmark["published_source"] != "x_id":
            changes.append(
                Change(
                    "bookmark",
                    bookmark["id"],
                    "published_source",
                    bookmark["published_source"],
                    "x_id",
                    group=group,
                    title=label(bookmark),
                    note="source: post ID",
                )
            )
    return changes
