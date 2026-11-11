"""Saved views: a named state of the interface that can be reopened later."""

from __future__ import annotations

import json
import sqlite3
import uuid

from atlas.db import now_iso

MAX_STATE_BYTES = 2_000_000
TABS = ("bookmarks", "map", "clusters", "grid", "tree")


def _clean_state(state) -> dict:
    """Keep only what a view may hold, so that a view coming from a snapshot
    made by someone else cannot carry anything unexpected."""
    if not isinstance(state, dict):
        raise ValueError("The view has no state to save.")
    tab = state.get("tab") if state.get("tab") in TABS else "bookmarks"

    def text(value, limit=500):
        return value[:limit] if isinstance(value, str) else ""

    def ids(value):
        return (
            [item for item in value if isinstance(item, str) and len(item) <= 64][:50_000]
            if isinstance(value, list)
            else []
        )

    def number(value, default):
        return (
            float(value)
            if isinstance(value, int | float) and not isinstance(value, bool)
            else default
        )

    cluster = state.get("cluster") if isinstance(state.get("cluster"), dict) else None
    map_state = state.get("map") if isinstance(state.get("map"), dict) else {}
    focus = map_state.get("focus")
    return {
        "tab": tab,
        "tree_path": [item for item in ids(state.get("tree_path")) if len(item) <= 20][:4],
        "search": text(state.get("search")),
        "cluster": {"key": text(cluster.get("key"), 100), "label": text(cluster.get("label"), 200)}
        if cluster and cluster.get("key")
        else None,
        "selection": ids(state.get("selection")),
        "selection_filter": bool(state.get("selection_filter")),
        "clusters_run": text(state.get("clusters_run"), 64) or None,
        "map": {
            "focus": int(focus) if isinstance(focus, int) and not isinstance(focus, bool) else None,
            "scale": min(40.0, max(0.6, number(map_state.get("scale"), 1.0))),
            "tx": number(map_state.get("tx"), 0.0),
            "ty": number(map_state.get("ty"), 0.0),
            "search": text(map_state.get("search")),
            "clusters_run": text(map_state.get("clusters_run"), 64) or None,
        },
    }


def save_view(conn: sqlite3.Connection, name: str, state, note: str | None = None) -> dict:
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ValueError("Give the view a name.")
    encoded = json.dumps(_clean_state(state), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("This view is too large to save.")
    view_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO views (id, name, note, created_at, state) VALUES (?, ?, ?, ?, ?)",
        (view_id, name, (note or "").strip()[:5000] or None, now_iso(), encoded),
    )
    conn.commit()
    return get_view(conn, view_id)


def _row(row: sqlite3.Row, with_state: bool) -> dict:
    state = _clean_state(json.loads(row["state"]))
    view = {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "tab": state["tab"],
        "selected": len(state["selection"]),
    }
    if with_state:
        view["state"] = state
    return view


def list_views(conn: sqlite3.Connection) -> list[dict]:
    return [
        _row(row, False)
        for row in conn.execute("SELECT * FROM views ORDER BY created_at DESC, rowid DESC")
    ]


def get_view(conn: sqlite3.Connection, view_id: str) -> dict:
    row = conn.execute("SELECT * FROM views WHERE id = ?", (view_id,)).fetchone()
    if row is None:
        raise ValueError("No such view.")
    return _row(row, True)


def delete_view(conn: sqlite3.Connection, view_id: str) -> None:
    if conn.execute("SELECT 1 FROM views WHERE id = ?", (view_id,)).fetchone() is None:
        raise ValueError("No such view.")
    conn.execute("DELETE FROM views WHERE id = ?", (view_id,))
    conn.commit()
