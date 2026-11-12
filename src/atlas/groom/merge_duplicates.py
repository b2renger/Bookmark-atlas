"""Fold duplicates into one bookmark."""

from collections import defaultdict

from atlas.dates import parse_date
from atlas.groom import NOW, Change
from atlas.groom.common import label, live_bookmarks, record_fields, resolve_tag, tags_of

NAME = "merge-duplicates"
TITLE = "Merge duplicates"
DESCRIPTION = """
Two things count as duplicates. First, rows that were imported for a link the
collection already had, from your table or from a bundle someone shared: the
import kept them aside. Second, two bookmarks whose links turn out to point to
the same page.

In both cases one bookmark is kept. It gains the tags of the others, keeps the
earliest save date, and the others are marked as merged into it. Titles and
summaries of the kept bookmark are not changed.
"""
VERSION = "1"
PARAMS = []


def _earlier(candidate: str | None, current: str | None) -> bool:
    return candidate is not None and (current is None or candidate < current)


def _held_aside(conn, changes):
    """Pending records that are a second row for a bookmark, not an edit of its own row."""
    for record in conn.execute(
        "SELECT r.* FROM source_records r JOIN bookmarks b ON b.id = r.bookmark_id "
        "WHERE r.pending = 1 AND b.deleted_at IS NULL AND (r.source = 'atlas' OR EXISTS ("
        "  SELECT 1 FROM source_records o WHERE o.bookmark_id = r.bookmark_id "
        "  AND o.source = r.source AND o.id != r.id)) ORDER BY r.id"
    ).fetchall():
        bookmark = conn.execute(
            "SELECT * FROM bookmarks WHERE id = ?", (record["bookmark_id"],)
        ).fetchone()
        fields = record_fields(conn, record)
        group = f"bookmark:{bookmark['id']}"
        title = label(bookmark)
        other = (fields["title"] or "").strip()
        note = "the duplicate row is folded in"
        if other and other != (bookmark["title"] or ""):
            note += f" (its title was “{other[:80]}”)"

        present = tags_of(conn, bookmark["id"]) | {
            change.new
            for change in changes
            if change.entity == "bookmark_tag" and change.entity_id == bookmark["id"]
        }
        for name in fields["tags"]:
            name = resolve_tag(conn, name)
            if name not in present:
                present.add(name)
                changes.append(
                    Change(
                        "bookmark_tag", bookmark["id"], "tag", None, name, group=group, title=title
                    )
                )

        parsed = parse_date(fields["date_text"])
        if (
            parsed is not None
            and not parsed.date_only
            and bookmark["saved_source"] in (None, "date_column")
            and _earlier(parsed.iso, bookmark["saved_at"])
            and not any(
                c.entity == "bookmark" and c.entity_id == bookmark["id"] and c.field == "saved_at"
                for c in changes
            )
        ):
            changes.append(
                Change(
                    "bookmark",
                    bookmark["id"],
                    "saved_at",
                    bookmark["saved_at"],
                    parsed.iso,
                    group=group,
                    title=title,
                    note=f"save date moves to the earlier one, {parsed.iso}",
                )
            )
            if bookmark["saved_source"] != "date_column":
                changes.append(
                    Change(
                        "bookmark",
                        bookmark["id"],
                        "saved_source",
                        bookmark["saved_source"],
                        "date_column",
                        group=group,
                        title=title,
                        note="source: Date column",
                    )
                )
        changes.append(
            Change(
                "source_record",
                str(record["id"]),
                "pending",
                "1",
                "0",
                group=group,
                title=title,
                note=note,
            )
        )


def _same_link(conn, changes):
    """Live bookmarks sharing a normalised link: the oldest one absorbs the others."""
    by_link = defaultdict(list)
    for bookmark in live_bookmarks(conn):
        by_link[bookmark["url_normalized"]].append(bookmark)
    for _link, group_members in sorted(by_link.items()):
        if len(group_members) < 2:
            continue
        keeper, *others = group_members  # live_bookmarks is oldest first
        absorb(conn, changes, keeper, others)


def absorb(conn, changes, keeper, others, why="is merged into this bookmark"):
    """The changes that fold ``others`` into ``keeper``: its tags and earliest
    save date move over, and the others leave the list."""
    group = f"bookmark:{keeper['id']}"
    title = label(keeper)
    present = tags_of(conn, keeper["id"])
    saved = keeper["saved_at"]
    for other in others:
        for name in sorted(tags_of(conn, other["id"])):
            if name not in present:
                present.add(name)
                changes.append(
                    Change(
                        "bookmark_tag",
                        keeper["id"],
                        "tag",
                        None,
                        name,
                        group=group,
                        title=title,
                    )
                )
        if _earlier(other["saved_at"], saved):
            saved = other["saved_at"]
        changes.append(
            Change(
                "bookmark",
                other["id"],
                "merged_into",
                None,
                keeper["id"],
                group=group,
                title=title,
                note=f"“{label(other)}” {why}",
            )
        )
        changes.append(
            Change(
                "bookmark",
                other["id"],
                "deleted_at",
                None,
                NOW,
                group=group,
                title=title,
                note="and leaves the list",
            )
        )
    if saved != keeper["saved_at"]:
        changes.append(
            Change(
                "bookmark",
                keeper["id"],
                "saved_at",
                keeper["saved_at"],
                saved,
                group=group,
                title=title,
                note=f"save date moves to the earliest one, {saved}",
            )
        )


def plan(conn, params):
    changes = []
    _same_link(conn, changes)
    _held_aside(conn, changes)
    return changes
