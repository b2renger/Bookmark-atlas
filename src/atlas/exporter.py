"""Reading the collection out: CSV and JSON export, and a status summary."""

from __future__ import annotations

import csv
import json
import sqlite3
from typing import TextIO

EXPORT_FIELDS = [
    "id",
    "url",
    "title",
    "summary",
    "tags",
    "domain",
    "platform",
    "saved_at",
    "published_at",
]


def export_bookmarks(conn: sqlite3.Connection, *, include_deleted: bool = False) -> list[dict]:
    """Curated bookmarks, oldest first, each with its tags as a sorted list."""
    tags: dict[str, list[str]] = {}
    for bookmark_id, name in conn.execute(
        "SELECT bookmark_id, name FROM bookmark_tags JOIN tags ON tags.id = tag_id "
        "ORDER BY name COLLATE NOCASE"
    ):
        tags.setdefault(bookmark_id, []).append(name)

    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = []
    for row in conn.execute(
        f"SELECT * FROM bookmarks {where} ORDER BY COALESCE(saved_at, created_at), id"
    ):
        item = {
            name: tags.get(row["id"], []) if name == "tags" else row[name] for name in EXPORT_FIELDS
        }
        if include_deleted:
            item["deleted_at"] = row["deleted_at"]
            item["merged_into"] = row["merged_into"]
        rows.append(item)
    return rows


def write_csv(rows: list[dict], handle: TextIO) -> None:
    """Tags are joined with ', ' in one column, as in the Notion export."""
    fields = list(rows[0].keys()) if rows else EXPORT_FIELDS
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "tags": ", ".join(row["tags"])})


def write_json(rows: list[dict], handle: TextIO) -> None:
    json.dump(rows, handle, ensure_ascii=False, indent=2)
    handle.write("\n")


def status(conn: sqlite3.Connection) -> dict:
    """Counts describing the state of the collection."""

    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    live = "deleted_at IS NULL"
    return {
        "bookmarks": one(f"SELECT COUNT(*) FROM bookmarks WHERE {live}"),
        "deleted": one("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NOT NULL"),
        "source_records": one("SELECT COUNT(*) FROM source_records"),
        "pending_review": one("SELECT COUNT(*) FROM source_records WHERE pending"),
        "tags": one("SELECT COUNT(*) FROM tags WHERE alias_of IS NULL"),
        "tags_used_once": one(
            "SELECT COUNT(*) FROM (SELECT tag_id FROM bookmark_tags "
            "GROUP BY tag_id HAVING COUNT(*) = 1)"
        ),
        "without_tags": one(
            f"SELECT COUNT(*) FROM bookmarks WHERE {live} "
            "AND id NOT IN (SELECT bookmark_id FROM bookmark_tags)"
        ),
        "without_saved_at": one(
            f"SELECT COUNT(*) FROM bookmarks WHERE {live} AND saved_at IS NULL"
        ),
        "without_published_at": one(
            f"SELECT COUNT(*) FROM bookmarks WHERE {live} AND published_at IS NULL"
        ),
        "without_platform": one(
            f"SELECT COUNT(*) FROM bookmarks WHERE {live} AND platform IS NULL"
        ),
        "top_domains": [
            (row[0], row[1])
            for row in conn.execute(
                f"SELECT domain, COUNT(*) AS n FROM bookmarks WHERE {live} "
                "GROUP BY domain ORDER BY n DESC, domain LIMIT 5"
            )
        ],
    }
