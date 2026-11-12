import json
import sqlite3

import pytest

np = pytest.importorskip("numpy")

from atlas import db, snapshot, views  # noqa: E402
from atlas.analysis import store  # noqa: E402
from atlas.cli import main  # noqa: E402
from test_clusters import fill  # noqa: E402


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "home" / "atlas.sqlite"
    target.parent.mkdir()
    main(["--db", str(target), "init"])
    return target


@pytest.fixture
def conn(path):
    connection = db.open_ready(path)
    fill(connection)
    yield connection
    connection.close()


STATE = {
    "tab": "map",
    "search": "splat",
    "cluster": {"key": "abc:2", "label": "Splats"},
    "selection": ["a", "b"],
    "selection_filter": True,
    "map": {
        "focus": 2,
        "scale": 2.5,
        "tx": -40,
        "ty": 12.5,
        "search": "audio",
        "clusters_run": "abc",
    },
}


def test_save_list_open_delete_a_view(conn):
    saved = views.save_view(conn, "  Splat  corner ", STATE, note="what I was reading in June")
    assert saved["name"] == "Splat corner" and saved["tab"] == "map" and saved["selected"] == 2
    assert saved["state"]["map"] == {
        "focus": 2,
        "scale": 2.5,
        "tx": -40.0,
        "ty": 12.5,
        "search": "audio",
        "clusters_run": "abc",
        "layout": "similarity",
    }
    listed = views.list_views(conn)
    assert [v["name"] for v in listed] == ["Splat corner"] and "state" not in listed[0]
    assert views.get_view(conn, saved["id"])["note"] == "what I was reading in June"

    views.delete_view(conn, saved["id"])
    assert views.list_views(conn) == []
    with pytest.raises(ValueError, match="No such view"):
        views.get_view(conn, saved["id"])


def test_a_view_keeps_only_what_a_view_may_hold(conn):
    hostile = {
        "tab": "settings",
        "search": "x" * 5000,
        "cluster": {"key": 7, "label": "<b>"},
        "selection": ["ok", 3, {"a": 1}, "y" * 100],
        "map": {"focus": "2", "scale": 1e9, "tx": "left", "ty": True, "extra": "<script>"},
        "__proto__": {"polluted": True},
        "html": "<img onerror=alert(1)>",
    }
    state = views.save_view(conn, "odd", hostile)["state"]
    assert state["tab"] == "bookmarks" and len(state["search"]) == 500
    assert state["cluster"] is None and state["selection"] == ["ok"]
    assert state["map"] == {
        "focus": None,
        "scale": 40.0,
        "tx": 0.0,
        "ty": 0.0,
        "search": "",
        "clusters_run": None,
        "layout": "similarity",
    }
    assert set(state) == {
        "facets",
        "threads",
        "tab",
        "tree_path",
        "search",
        "cluster",
        "selection",
        "selection_filter",
        "clusters_run",
        "map",
    }
    with pytest.raises(ValueError, match="name"):
        views.save_view(conn, "   ", STATE)
    with pytest.raises(ValueError, match="no state"):
        views.save_view(conn, "x", "not a dict")


def test_snapshot_carries_the_exploration_and_nothing_private(conn, tmp_path):
    run = store.cluster_run(conn, 3)
    store.set_cluster_label(conn, run["run_id"], run["clusters"][0]["cluster"], "Named by me")
    store.project_run(conn)
    views.save_view(conn, "My view", STATE)
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE url LIKE '%/audio/0'")
    conn.commit()

    target = tmp_path / "share" / ("all" + snapshot.SUFFIX)
    info = snapshot.export_snapshot(conn, target, name="June reading", note="for Zaki")
    assert info["bookmarks"] == 71 and not info["partial"] and info["views"] == 1

    other = db.open_ready(target)  # it is an ordinary database
    assert snapshot.snapshot_info(other) == {
        "name": "June reading",
        "note": "for Zaki",
        "created_at": info["created_at"],
        "bookmarks": 71,
        "partial": False,
    }
    assert other.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 71
    for table in ("source_records", "changes", "script_runs", "sync_state"):
        assert other.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # Clusters, labels, map and views are there and usable without recomputing.
    shown = store.describe_run(other, run["run_id"])
    assert sum(c["size"] for c in shown["clusters"]) == 71
    assert any(c["label"] == "Named by me" for c in shown["clusters"])
    data = store.map_data(other)
    assert len(data["points"]) == 71 and data["missing"] == 0 and len(data["clusters"]) == 3
    assert [v["name"] for v in views.list_views(other)] == ["My view"]
    assert store.vectors_are_current(other)
    other.close()
    assert snapshot.snapshot_info(conn) is None  # the original is not a snapshot


def test_partial_snapshot_holds_only_the_chosen_bookmarks(conn, tmp_path):
    store.cluster_run(conn, 3)
    store.project_run(conn)
    chosen = [r[0] for r in conn.execute("SELECT id FROM bookmarks WHERE url LIKE '%/splats/%'")]

    target = tmp_path / ("part" + snapshot.SUFFIX)
    info = snapshot.export_snapshot(conn, target, ids=chosen)
    assert info["bookmarks"] == 24 and info["partial"]

    other = db.open_ready(target)
    assert other.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 24
    assert other.execute("SELECT COUNT(*) FROM run_items").fetchone()[0] == 48  # cluster + map rows
    names = {r[0] for r in other.execute("SELECT name FROM tags")}
    assert names == {"Splats", "Tool"}  # tags of the others did not travel
    raw = target.read_bytes()
    assert b"example.org/audio/" not in raw and b"synthesizer" not in raw
    assert len(store.map_data(other)["points"]) == 24
    other.close()


def test_snapshot_refuses_to_overwrite_or_to_be_empty(conn, tmp_path):
    target = tmp_path / ("x" + snapshot.SUFFIX)
    snapshot.export_snapshot(conn, target)
    with pytest.raises(ValueError, match="already exists"):
        snapshot.export_snapshot(conn, target)
    empty = tmp_path / ("empty" + snapshot.SUFFIX)
    with pytest.raises(ValueError, match="nothing to put"):
        snapshot.export_snapshot(conn, empty, ids=["no-such-id"])
    assert not empty.exists()


def test_a_tampered_snapshot_description_is_read_defensively(conn):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('snapshot', ?)",
        ('{"name": 5, "bookmarks": "many", "note": ["x"]}',),
    )
    info = snapshot.snapshot_info(conn)
    assert info["name"] == "5" and info["bookmarks"] is None and isinstance(info["note"], str)
    conn.execute("UPDATE meta SET value = 'not json' WHERE key = 'snapshot'")
    assert snapshot.snapshot_info(conn) is None


def test_cli_snapshot_and_open(path, tmp_path, capsys):
    conn = db.open_ready(path)
    fill(conn)
    conn.close()
    out = tmp_path / ("cli" + snapshot.SUFFIX)
    assert (
        main(
            [
                "--db",
                str(path),
                "snapshot",
                "-o",
                str(out),
                "--search",
                "splats",
                "--name",
                "Splats only",
            ]
        )
        == 0
    )
    assert "snapshot of 24 bookmarks" in capsys.readouterr().out
    assert main(["--db", str(out), "status"]) == 0
    assert "bookmarks                     24" in capsys.readouterr().out


def test_views_and_snapshot_through_the_api(conn, path, tmp_path, monkeypatch):
    import threading
    import urllib.request

    from atlas.server import App, make_server

    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    store.cluster_run(conn, 3)
    app = App(path)
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, route, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Atlas-Token": app.session_token, "Content-Type": "application/json"}
        request = urllib.request.Request(base + route, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert call("GET", "/api/state")[1]["snapshot"] is None
        code, answer = call("POST", "/api/views", {"name": "Mine", "state": STATE, "note": "n"})
        assert code == 200 and answer["views"][0]["name"] == "Mine"
        view_id = answer["view"]["id"]
        assert call("GET", f"/api/views/item?id={view_id}")[1]["view"]["state"]["search"] == "splat"
        assert call("POST", "/api/views", {"name": "", "state": STATE})[0] == 400

        code, blob = call("POST", "/api/snapshot", {"name": "Shared"}, raw=True)
        assert code == 200 and blob[:16] == b"SQLite format 3\x00"
        received = tmp_path / ("received" + snapshot.SUFFIX)
        received.write_bytes(blob)
        other = sqlite3.connect(received)
        assert other.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 72
        assert other.execute("SELECT name FROM views").fetchone()[0] == "Mine"
        other.close()
        assert call("POST", "/api/snapshot", {"ids": []})[0] == 400
        assert call("POST", "/api/snapshot", {"ids": ["nope"]})[0] == 400

        assert call("POST", "/api/views/delete", {"id": view_id})[1] == {"views": []}
        assert call("POST", "/api/views/delete", {"id": view_id})[0] == 400

        # A second Atlas started on the received file shows it as a snapshot.
        second = App(received)
        assert second.state()["snapshot"]["name"] == "Shared"
    finally:
        server.shutdown()
        server.server_close()
