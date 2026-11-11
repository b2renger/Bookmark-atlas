"""Helpers shared by the built-in grooming scripts."""

from __future__ import annotations

import json
import sqlite3

from atlas.importer import DEFAULT_COLUMNS, split_tags


def live_bookmarks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bookmarks WHERE deleted_at IS NULL ORDER BY created_at, id"
    ).fetchall()


def label(bookmark: sqlite3.Row) -> str:
    """How a bookmark is named in a preview."""
    return (bookmark["title"] or bookmark["url"] or bookmark["id"])[:120]


def tags_of(conn: sqlite3.Connection, bookmark_id: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id WHERE bookmark_id = ?",
            (bookmark_id,),
        )
    }


def resolve_tag(conn: sqlite3.Connection, name: str) -> str:
    """Name of the tag ``name`` has been merged into, or ``name`` itself."""
    seen = {name}
    while True:
        row = conn.execute(
            "SELECT a.name FROM tags t JOIN tags a ON a.id = t.alias_of WHERE t.name = ?", (name,)
        ).fetchone()
        if row is None or row[0] in seen:
            return name
        name = row[0]
        seen.add(name)


def record_fields(conn: sqlite3.Connection, record: sqlite3.Row) -> dict:
    """Title, URL, summary, tags and date text of a source record, whatever its origin."""
    raw = json.loads(record["raw"])
    if record["source"] == "atlas":  # a shared bundle: fields are already named
        return {
            "title": raw.get("title"),
            "url": raw.get("url"),
            "summary": raw.get("summary"),
            "tags": list(raw.get("tags") or []),
            "date_text": None,
        }
    if record["source"] == "notion":
        from atlas.notion_sync import extract, selected

        choice = selected(conn)
        mapping = choice["property_map"] if choice else {}
        return extract(raw, mapping)
    by_lower = {key.lower(): key for key in raw}

    def column(local: str) -> str | None:
        for candidate in DEFAULT_COLUMNS[local]:
            if candidate.lower() in by_lower:
                return raw.get(by_lower[candidate.lower()])
        return None

    return {
        "title": column("title"),
        "url": column("url"),
        "summary": column("summary"),
        "tags": split_tags(column("tags")),
        "date_text": column("date"),
    }
