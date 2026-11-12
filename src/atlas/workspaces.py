"""Workspaces: independent copies of part of the collection, to work on apart.

A workspace is an ordinary Atlas database in its own folder,
``Workspaces/<name>/atlas.sqlite`` beside the main collection. It starts as a
copy of some bookmarks (or empty) and then lives its own life: its own tags,
clusters, map, views, grooming history and Notion table. Nothing done in a
workspace touches the main collection until you merge it back, and merging
only adds: new bookmarks, and optionally the tags a workspace gave to
bookmarks both sides have.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from atlas import db, groom, snapshot
from atlas.db import now_iso
from atlas.groom import Change

FOLDER = "Workspaces"


def root(home: Path | str) -> Path:
    """Where the workspaces of a collection live: beside its database."""
    return Path(home).resolve().parent / FOLDER


def path_of(home: Path | str, workspace_id: str) -> Path:
    """The database of a workspace. Refuses anything that is not a plain folder name."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", str(workspace_id or "")):
        raise ValueError("No such workspace.")
    target = root(home) / workspace_id / "atlas.sqlite"
    if not target.is_file():
        raise ValueError("No such workspace.")
    return target


def info(conn: sqlite3.Connection) -> dict | None:
    """What a database says about itself if it is a workspace."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'workspace'").fetchone()
        data = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "name": str(data.get("name") or "")[:120],
        "created_at": str(data.get("created_at") or "")[:40],
        "started_from": str(data.get("started_from") or "")[:200],
    }


def list_all(home: Path | str) -> list[dict]:
    found = []
    folder = root(home)
    if not folder.is_dir():
        return found
    for target in sorted(folder.glob("*/atlas.sqlite")):
        try:
            conn = db.open_ready(target)
        except Exception:  # a damaged or foreign file is not listed, and not touched
            continue
        try:
            described = info(conn)
            if described is None:
                continue
            count = conn.execute(
                "SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        found.append({"id": target.parent.name, **described, "bookmarks": count})
    found.sort(key=lambda item: item["created_at"], reverse=True)
    return found


def _slug(home: Path | str, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"
    slug, number = base, 2
    while (root(home) / slug).exists():
        slug, number = f"{base}-{number}", number + 1
    return slug


def create(
    source: sqlite3.Connection,
    home: Path | str,
    name: str,
    *,
    ids: list[str] | None = None,
    empty: bool = False,
    started_from: str = "",
) -> dict:
    """Make a workspace from the bookmarks of ``source`` (all of them, those in
    ``ids``, or none with ``empty``). Clusters, maps and views that concern the
    copied bookmarks come along."""
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ValueError("Give the workspace a name.")
    slug = _slug(home, name)
    target = root(home) / slug / "atlas.sqlite"
    target.parent.mkdir(parents=True, exist_ok=False)
    try:
        if empty:
            conn = db.connect(target, create=True)
            db.migrate(conn)
        else:
            snapshot.export_snapshot(source, target, ids=ids, name=name)
            conn = db.open_ready(target)
            conn.execute("DELETE FROM meta WHERE key = 'snapshot'")
        described = {"name": name, "created_at": now_iso(), "started_from": started_from[:200]}
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('workspace', ?)",
            (json.dumps(described, ensure_ascii=False),),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL").fetchone()[
            0
        ]
        conn.close()
    except BaseException:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise
    return {"id": slug, **described, "bookmarks": count}


def delete(home: Path | str, workspace_id: str) -> None:
    """Remove a workspace and everything in its folder."""
    target = path_of(home, workspace_id)
    shutil.rmtree(target.parent)


def merge(
    home_conn: sqlite3.Connection,
    home: Path | str,
    workspace_id: str,
    *,
    add_tags: bool = True,
) -> dict:
    """Bring a workspace back into the main collection. Only adds.

    Bookmarks the main collection lacks arrive as new ones (what was deleted
    there stays deleted). With ``add_tags``, bookmarks both sides have also
    receive the tags the workspace gave them, as one run that can be undone
    from the grooming history.
    """
    target = path_of(home, workspace_id)
    before = snapshot.compare(home_conn, target, limit=100_000)
    report = snapshot.adopt(home_conn, target)
    home_conn.commit()
    tagged = 0
    if add_tags and before["tag_differences"]["items"]:
        changes = []
        for item in before["tag_differences"]["items"]:
            for tag in item["only_theirs"]:
                changes.append(
                    Change(
                        "bookmark_tag",
                        item["id"],
                        "tag",
                        None,
                        tag,
                        group=f"bookmark:{item['id']}",
                        title=item["title"][:120],
                    )
                )
        if changes:
            name = (before["snapshot"] or {}).get("name") or workspace_id
            groom.apply_changes(home_conn, changes, label=f"merge workspace “{name}”", db_path=home)
            tagged = len({change.entity_id for change in changes})
    return {
        "added": report.created,
        "tagged": tagged,
        "kept_deleted": before["deleted_here"],
        "already_there": before["in_both"],
    }
