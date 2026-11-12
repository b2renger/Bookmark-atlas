"""Reading the collection out: CSV and JSON export, and a status summary."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
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


DATE_KINDS = {
    "when": "COALESCE(saved_at, published_at)",
    "saved": "saved_at",
    "published": "published_at",
}


def facet_clause(facets: dict | None) -> tuple[str, list]:
    """Extra conditions for the shared filter: tags (all of them), platform,
    and a range of days on the saved date, the published date, or the saved
    date falling back on the published one (``date_kind`` "when")."""
    where, params = "", []
    facets = facets or {}
    for tag in [t for t in (facets.get("tags") or []) if str(t).strip()][:10]:
        where += (
            " AND id IN (SELECT bookmark_id FROM bookmark_tags JOIN tags ON tags.id = tag_id "
            "WHERE name = ? COLLATE NOCASE)"
        )
        params.append(str(tag).strip())
    platform = str(facets.get("platform") or "").strip()
    if platform:
        where += " AND platform = ? COLLATE NOCASE"
        params.append(platform)
    column = DATE_KINDS.get(str(facets.get("date_kind") or "when"))
    if column is None:
        raise ValueError("The date to filter on is one of: when, saved, published.")
    for key, operator in (("date_from", ">="), ("date_to", "<=")):
        day = str(facets.get(key) or "").strip()
        if not day:
            continue
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("Dates in a filter look like 2026-03-15.") from None
        where += f" AND substr({column}, 1, 10) {operator} ?"
        params.append(day)
    return where, params


def facet_options(conn: sqlite3.Connection) -> dict:
    """What the filter bar offers: the most used tags, the platforms, the span of dates."""
    tags = [
        {"name": row[0], "count": row[1]}
        for row in conn.execute(
            "SELECT t.name, COUNT(*) AS n FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id "
            "JOIN bookmarks b ON b.id = bt.bookmark_id WHERE b.deleted_at IS NULL "
            "GROUP BY t.id ORDER BY n DESC, t.name COLLATE NOCASE LIMIT 200"
        )
    ]
    platforms = [
        {"name": row[0], "count": row[1]}
        for row in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM bookmarks WHERE deleted_at IS NULL "
            "AND platform IS NOT NULL AND platform != '' GROUP BY platform "
            "ORDER BY n DESC, platform COLLATE NOCASE"
        )
    ]
    span = conn.execute(
        "SELECT MIN(substr(COALESCE(saved_at, published_at), 1, 10)), "
        "MAX(substr(COALESCE(saved_at, published_at), 1, 10)) FROM bookmarks "
        "WHERE deleted_at IS NULL"
    ).fetchone()
    months = {}
    for kind, column in DATE_KINDS.items():
        months[kind] = [
            [row[0], row[1]]
            for row in conn.execute(
                f"SELECT substr({column}, 1, 7) AS month, COUNT(*) FROM bookmarks "
                f"WHERE deleted_at IS NULL AND {column} IS NOT NULL GROUP BY month ORDER BY month"
            )
        ]
    return {
        "tags": tags,
        "platforms": platforms,
        "first_day": span[0],
        "last_day": span[1],
        "months": months,  # for the date brush: bookmarks per month, for each kind of date
    }


def search_clause(
    text: str | None, cluster: str | None = None, facets: dict | None = None
) -> tuple[str, list]:
    """WHERE clause and parameters for live bookmarks matching a search text
    and, optionally, belonging to one cluster of a run (``"<run id>:<number>"``)
    and passing the shared filter (see ``facet_clause``)."""
    where, params = "deleted_at IS NULL", []
    if cluster:
        run_id, _, number = cluster.partition(":")
        try:
            number = int(number)
        except ValueError:
            raise ValueError("A cluster filter looks like <run id>:<number>.") from None
        where += " AND id IN (SELECT bookmark_id FROM run_items WHERE run_id = ? AND cluster = ?)"
        params = [run_id, number]
    text = (text or "").strip()
    if text:
        like = f"%{text}%"
        where += (
            " AND (title LIKE ? OR summary LIKE ? OR url LIKE ? OR platform LIKE ? OR id IN ("
            "SELECT bookmark_id FROM bookmark_tags JOIN tags ON tags.id = tag_id "
            "WHERE name LIKE ?))"
        )
        params = [*params, like, like, like, like, like]
    extra, extra_params = facet_clause(facets)
    return where + extra, [*params, *extra_params]


def matching_ids(
    conn: sqlite3.Connection,
    text: str | None,
    cluster: str | None = None,
    facets: dict | None = None,
) -> list[str]:
    where, params = search_clause(text, cluster, facets)
    return [row[0] for row in conn.execute(f"SELECT id FROM bookmarks WHERE {where}", params)]


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
        "tags": one(
            "SELECT COUNT(DISTINCT tag_id) FROM bookmark_tags JOIN bookmarks ON id = bookmark_id "
            f"WHERE {live}"
        ),
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
