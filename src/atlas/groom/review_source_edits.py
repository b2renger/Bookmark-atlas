"""Deal with bookmarks that were edited in Notion after their import."""

from atlas.groom import Change
from atlas.groom.common import label, record_fields, resolve_tag, tags_of

NAME = "review-source-edits"
TITLE = "Review what changed in Notion"
DESCRIPTION = """
When a row you already imported is edited in Notion, the sync keeps the new
version aside and leaves your bookmark alone. This script lists those rows
with what differs. Choose "take" to bring Notion's title, summary and tags
into the bookmarks you tick, or "keep" to mark them as seen and stay with
your version. Run it twice if you want some of each.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "decision",
        "type": "choice",
        "default": "take",
        "label": "For the rows I leave ticked",
        "choices": [
            {"value": "take", "label": "take Notion's version"},
            {"value": "keep", "label": "keep my version and mark as seen"},
        ],
    },
    {
        "name": "remove_tags",
        "type": "bool",
        "default": False,
        "label": "When taking Notion's version, also remove tags that are no longer there",
    },
]


def edited_records(conn):
    """Pending records that are a new version of a bookmark's own row (a second
    row for the same link is a duplicate, handled by merge-duplicates)."""
    return conn.execute(
        "SELECT r.* FROM source_records r JOIN bookmarks b ON b.id = r.bookmark_id "
        "WHERE r.pending = 1 AND b.deleted_at IS NULL AND r.source != 'atlas' AND NOT EXISTS ("
        "  SELECT 1 FROM source_records o WHERE o.bookmark_id = r.bookmark_id "
        "  AND o.source = r.source AND o.id != r.id) ORDER BY r.id"
    ).fetchall()


def _short(value) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= 90 else value[:89] + "…"


def plan(conn, params):
    take = params["decision"] == "take"
    changes = []
    for record in edited_records(conn):
        bookmark = conn.execute(
            "SELECT * FROM bookmarks WHERE id = ?", (record["bookmark_id"],)
        ).fetchone()
        fields = record_fields(conn, record)
        group = f"bookmark:{bookmark['id']}"
        title = label(bookmark)
        differs = False
        for field in ("title", "summary"):
            new = (fields.get(field) or "").strip() or None
            if new is None or new == bookmark[field]:
                continue
            differs = True
            if take:
                changes.append(
                    Change(
                        "bookmark",
                        bookmark["id"],
                        field,
                        bookmark[field],
                        new,
                        group=group,
                        title=title,
                        note=f"{field}: “{_short(bookmark[field])}” → “{_short(new)}”",
                    )
                )
        mine = tags_of(conn, bookmark["id"])
        theirs = {resolve_tag(conn, name) for name in fields.get("tags") or []}
        added, gone = sorted(theirs - mine), sorted(mine - theirs)
        differs = differs or bool(added) or bool(gone)
        if take:
            for name in added:
                changes.append(
                    Change(
                        "bookmark_tag", bookmark["id"], "tag", None, name, group=group, title=title
                    )
                )
            if params["remove_tags"]:
                for name in gone:
                    changes.append(
                        Change(
                            "bookmark_tag",
                            bookmark["id"],
                            "tag",
                            name,
                            None,
                            group=group,
                            title=title,
                        )
                    )
        if take:
            note = (
                "marked as reviewed"
                if differs
                else "nothing differs in title, summary or tags: marked as reviewed"
            )
        else:
            parts = []
            if added:
                parts.append("Notion adds " + ", ".join(added[:6]))
            if gone:
                parts.append("Notion no longer has " + ", ".join(gone[:6]))
            for field in ("title", "summary"):
                new = (fields.get(field) or "").strip() or None
                if new is not None and new != bookmark[field]:
                    parts.append(f"Notion's {field}: “{_short(new)}”")
            note = "kept as it is, marked as seen" + (
                " (" + "; ".join(parts) + ")" if parts else ""
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
    return changes
