"""Editing several bookmarks at once.

Edits made by hand go through the grooming engine, so each batch is one
journaled run: backed up first, all-or-nothing, and undoable from the history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from atlas import groom
from atlas.groom import NOW, Change
from atlas.groom.common import label, resolve_tag, tags_of

MAX_SELECTION = 20_000
MAX_TAG_LENGTH = 80


def clean_tags(names) -> list[str]:
    """Tag names as the user typed them, trimmed and checked."""
    cleaned = []
    for name in names or []:
        if not isinstance(name, str):
            raise ValueError("A tag must be text.")
        name = " ".join(name.split())
        if not name:
            continue
        if "," in name:
            raise ValueError(f"A tag cannot contain a comma: {name!r}. Add tags one by one.")
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f"A tag is limited to {MAX_TAG_LENGTH} characters.")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _selected(conn: sqlite3.Connection, ids) -> list[sqlite3.Row]:
    if not isinstance(ids, list) or not ids:
        raise ValueError("Select at least one bookmark.")
    if len(ids) > MAX_SELECTION:
        raise ValueError(f"A selection is limited to {MAX_SELECTION} bookmarks.")
    wanted = [str(item) for item in dict.fromkeys(ids)]
    rows = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT * FROM bookmarks WHERE deleted_at IS NULL AND id IN ({marks})", chunk
        ):
            rows[row["id"]] = row
    return [rows[item] for item in wanted if item in rows]


def tag_counts(conn: sqlite3.Connection, ids) -> list[dict]:
    """Tags carried by the selection, most frequent first."""
    counts: dict[str, int] = {}
    for bookmark in _selected(conn, ids):
        for name in tags_of(conn, bookmark["id"]):
            counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"name": name, "count": count} for name, count in ordered]


def batch_changes(
    conn: sqlite3.Connection, ids, *, add_tags=(), remove_tags=(), delete: bool = False
) -> list[Change]:
    add = [resolve_tag(conn, name) for name in clean_tags(add_tags)]
    remove = clean_tags(remove_tags)
    if set(add) & set(remove):
        raise ValueError("The same tag is both added and removed.")
    if not add and not remove and not delete:
        raise ValueError("Nothing to do: give a tag to add or remove.")

    changes = []
    for bookmark in _selected(conn, ids):
        group = f"bookmark:{bookmark['id']}"
        title = label(bookmark)
        if delete:
            changes.append(
                Change(
                    "bookmark", bookmark["id"], "deleted_at", None, NOW, group=group, title=title
                )
            )
            continue
        present = tags_of(conn, bookmark["id"])
        for name in add:
            if name not in present:
                changes.append(
                    Change(
                        "bookmark_tag", bookmark["id"], "tag", None, name, group=group, title=title
                    )
                )
        for name in remove:
            if name in present:
                changes.append(
                    Change(
                        "bookmark_tag", bookmark["id"], "tag", name, None, group=group, title=title
                    )
                )
    return changes


def apply_batch(
    conn: sqlite3.Connection,
    ids,
    *,
    add_tags=(),
    remove_tags=(),
    delete: bool = False,
    db_path: Path | str | None = None,
) -> dict:
    changes = batch_changes(conn, ids, add_tags=add_tags, remove_tags=remove_tags, delete=delete)
    return groom.apply_changes(
        conn,
        changes,
        label="delete-selection" if delete else "edit-tags",
        params={
            "bookmarks": len(set(ids)),
            "add": clean_tags(add_tags),
            "remove": clean_tags(remove_tags),
        },
        db_path=db_path,
    )
