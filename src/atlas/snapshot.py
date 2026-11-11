"""Snapshots: a collection, or part of one, with its analysis and saved views,
as a single database file that another Atlas can open.

A snapshot is an ordinary Atlas database, so it opens like one. It holds the
bookmarks in scope with their tags, the clusters, the map, the text vectors
and the saved views. It leaves out everything private or irrelevant to the
receiver: the records as they came from Notion, the grooming journal, the
sync state. The Notion token is never in a database in the first place.

A bundle (see ``atlas.bundle``) shares bookmarks to *merge into* someone's
collection. A snapshot shares an *exploration* to look at as it was.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from atlas import __version__, db
from atlas.db import now_iso

SUFFIX = ".atlas-snapshot.sqlite"


def export_snapshot(
    conn: sqlite3.Connection,
    target: Path | str,
    *,
    ids: list[str] | None = None,
    name: str | None = None,
    note: str | None = None,
) -> dict:
    """Write a snapshot of the live bookmarks (all, or those in ``ids``)."""
    target = Path(target)
    if target.exists():
        raise ValueError(f"{target} already exists.")
    wanted = None if ids is None else {str(item) for item in ids}
    bookmarks = [
        row
        for row in conn.execute("SELECT * FROM bookmarks WHERE deleted_at IS NULL ORDER BY id")
        if wanted is None or row["id"] in wanted
    ]
    if not bookmarks:
        raise ValueError("There is nothing to put in the snapshot.")
    kept = {row["id"] for row in bookmarks}

    target.parent.mkdir(parents=True, exist_ok=True)
    out = db.connect(target, create=True)
    try:
        db.migrate(out)

        def copy(table: str, rows, columns: list[str]) -> int:
            rows = [tuple(row[column] for column in columns) for row in rows]
            marks = ",".join("?" * len(columns))
            out.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", rows)
            return len(rows)

        columns = [
            "id", "url", "url_normalized", "domain", "platform", "title", "summary", "saved_at",
            "saved_source", "published_at", "published_source", "created_at", "updated_at",
        ]  # fmt: skip
        copy("bookmarks", bookmarks, columns)

        links = [
            row
            for row in conn.execute("SELECT bookmark_id, tag_id FROM bookmark_tags")
            if row["bookmark_id"] in kept
        ]
        used = {row["tag_id"] for row in links}
        copy(
            "tags",
            [r for r in conn.execute("SELECT id, name FROM tags") if r["id"] in used],
            ["id", "name"],
        )
        copy("bookmark_tags", links, ["bookmark_id", "tag_id"])
        copy(
            "embeddings",
            [r for r in conn.execute("SELECT * FROM embeddings") if r["bookmark_id"] in kept],
            ["bookmark_id", "model", "dim", "vector", "text_hash"],
        )

        runs = conn.execute("SELECT * FROM runs").fetchall()
        copy(
            "runs",
            runs,
            ["id", "created_at", "kind", "algorithm", "params", "app_version", "metrics", "name"],
        )
        copy(
            "run_items",
            [r for r in conn.execute("SELECT * FROM run_items") if r["bookmark_id"] in kept],
            ["run_id", "bookmark_id", "cluster", "x", "y"],
        )
        copy(
            "run_artifacts", conn.execute("SELECT * FROM run_artifacts"), ["run_id", "name", "data"]
        )
        copy(
            "cluster_edits",
            conn.execute("SELECT * FROM cluster_edits"),
            ["run_id", "cluster", "label", "note", "merged_into"],
        )
        views = copy(
            "views",
            conn.execute("SELECT * FROM views"),
            ["id", "name", "note", "created_at", "state"],
        )
        copy(
            "annotations",
            [
                r
                for r in conn.execute("SELECT * FROM annotations")
                if r["bookmark_id"] is None or r["bookmark_id"] in kept
            ],
            ["id", "view_id", "bookmark_id", "body", "created_at"],
        )

        info = {
            "name": " ".join((name or "").split())[:120] or target.name,
            "note": (note or "").strip()[:5000],
            "created_at": now_iso(),
            "app_version": __version__,
            "bookmarks": len(bookmarks),
            "partial": wanted is not None,
            "views": views,
        }
        out.execute(
            "INSERT INTO meta (key, value) VALUES ('snapshot', ?)",
            (json.dumps(info, ensure_ascii=False),),
        )
        out.commit()
        out.execute("VACUUM")
    except BaseException:
        out.close()
        target.unlink(missing_ok=True)
        raise
    out.close()
    info["path"] = str(target)
    info["bytes"] = target.stat().st_size
    return info


def snapshot_info(conn: sqlite3.Connection) -> dict | None:
    """What a database says about itself if it was made as a snapshot."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'snapshot'").fetchone()
    if row is None:
        return None
    try:
        info = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(info, dict):
        return None
    return {
        "name": str(info.get("name") or "")[:120],
        "note": str(info.get("note") or "")[:5000],
        "created_at": str(info.get("created_at") or "")[:40],
        "bookmarks": info.get("bookmarks") if isinstance(info.get("bookmarks"), int) else None,
        "partial": bool(info.get("partial")),
    }
