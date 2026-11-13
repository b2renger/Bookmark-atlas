import pytest

np = pytest.importorskip("numpy")

from atlas import db, snapshot, views  # noqa: E402
from atlas.analysis import som, store, tree  # noqa: E402
from atlas.server import App  # noqa: E402
from test_clusters import fill, path  # noqa: E402, F401


def blobs(seed=0):
    rng = np.random.default_rng(seed)
    centres = np.array([[0, 0, 0], [8, 0, 0], [0, 8, 0], [8, 8, 0]], dtype=float)
    return np.vstack([c + rng.normal(0, 0.4, (40, 3)) for c in centres]), np.repeat(
        np.arange(4), 40
    )


def test_grid_shape_grows_with_the_collection():
    assert som.grid_shape(10) == (3, 4)
    rows, cols = som.grid_shape(956)
    assert (rows, cols) == (8, 11)
    assert som.grid_shape(100_000) == (16, 20)


def test_grid_is_repeatable_and_keeps_groups_together():
    x, truth = blobs()
    first = som.train(x, 4, 4)
    assert np.array_equal(first, som.train(x, 4, 4))
    cells = som.assign(x, first)
    # No cell mixes two blobs, and each blob stays in one corner of the grid.
    for cell in np.unique(cells):
        assert len(set(truth[cells == cell])) == 1
    for blob in range(4):
        rows, cols = cells[truth == blob] // 4, cells[truth == blob] % 4
        assert rows.max() - rows.min() <= 1 and cols.max() - cols.min() <= 1
    quality = som.errors(x, first, 4, 4)
    assert quality["quantization_error"] < 1.0
    assert 0.0 <= quality["topographic_error"] <= 0.2


def test_grid_run_places_every_bookmark_once(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    assert store.grid_data(conn) == {"grid": None, "cells": [], "missing": 72}
    data = store.grid_run(conn)
    assert (data["grid"]["rows"], data["grid"]["cols"]) == (3, 4)
    assert sum(cell["size"] for cell in data["cells"]) == 72 and data["missing"] == 0
    placed = [bookmark for cell in data["cells"] for bookmark in cell["ids"]]
    assert len(placed) == len(set(placed)) == 72
    # A cell holds one topic: the topic tag is among its tags. (The name itself is
    # chosen to tell the cell apart from its neighbours, which share that tag.)
    for cell in data["cells"]:
        if cell["size"]:
            parts = " · ".join(cell["tags"]).split(" · ")
            assert any(tag in ("Splats", "Language models", "Audio") for tag in parts)
    # Drawing again replaces the grid; a deleted bookmark leaves it.
    again = store.grid_run(conn)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'som'").fetchone()[0] == 1
    assert [c["ids"] for c in again["cells"]] == [c["ids"] for c in data["cells"]]
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE id = ?", (placed[0],))
    conn.commit()
    assert sum(cell["size"] for cell in store.grid_data(conn)["cells"]) == 71


def test_ward_groups_join_the_closest_centres():
    centres = [[0, 0], [0.5, 0], [10, 0], [10.5, 0], [30, 0]]
    assert sorted(tree.ward_groups(np, centres, [5, 5, 5, 5, 5], 3)) == [[0, 1], [2, 3], [4]]
    assert tree.ward_groups(np, centres, [5] * 5, 1) == [[0, 1, 2, 3, 4]]


def test_tree_covers_every_bookmark_and_can_be_edited(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn, per_topic=30)
    run = store.cluster_run(conn, k=3, algorithm="k-means")
    data = store.tree_run(conn, run["run_id"])
    root = data["tree"]
    assert root["size"] == 90 and data["missing"] == 0 and data["cluster_run"] == run["run_id"]
    assert sum(family["size"] for family in root["children"]) == 90
    topics = [topic for family in root["children"] for topic in family["children"]]
    assert sorted(topic["size"] for topic in topics) == [30, 30, 30]
    for topic in topics:  # 30 bookmarks: split into parts that add up
        assert topic["children"] and sum(part["size"] for part in topic["children"]) == 30
    everything = [b for topic in topics for b in topic["ids"]]
    assert len(set(everything)) == 90

    topic = topics[0]
    store.edit_tree(conn, "rename", node=topic["id"], name="  My   topic ")
    home = next(f for f in root["children"] if topic["id"] in [t["id"] for t in f["children"]])
    other = next(f for f in root["children"] if f["id"] != home["id"])
    after = store.edit_tree(conn, "move", node=topic["id"], family=other["id"])["tree"]
    moved, parent = tree.find(after, topic["id"])
    assert moved["name"] == "My topic" and parent["id"] == other["id"]
    assert after["size"] == 90 and sum(f["size"] for f in after["children"]) == 90
    # Two families of 3 topics: the emptied or shrunk one is consistent.
    assert all(f["children"] for f in after["children"])

    with pytest.raises(ValueError):
        store.edit_tree(conn, "rename", node="root", name="x")
    with pytest.raises(ValueError):
        store.edit_tree(conn, "rename", node=topic["id"], name="   ")
    with pytest.raises(ValueError):
        store.edit_tree(conn, "move", node=other["id"], family=other["id"])
    with pytest.raises(ValueError):
        store.edit_tree(conn, "explode")

    # A deleted bookmark leaves the tree and the counts follow.
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE id = ?", (everything[0],))
    conn.commit()
    assert store.tree_data(conn)["tree"]["size"] == 89
    # Building again starts over from the clustering.
    assert tree.find(store.tree_run(conn)["tree"], topic["id"])[0]["name"] != "My topic"


def test_tree_without_a_clustering_makes_one(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    assert store.tree_data(conn) == {"tree": None}
    with pytest.raises(ValueError):
        store.edit_tree(conn, "rename", node="t0", name="x")
    assert store.tree_run(conn)["tree"]["size"] == 72
    assert store.latest_run_id(conn) is not None


def test_grid_and_tree_through_the_api_and_in_a_snapshot(path, tmp_path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    app = App(path)
    assert app.grid_show()["grid"] is None and app.tree_show() == {"tree": None}
    cells = app.grid_new()["cells"]
    built = app.tree_new({})["tree"]
    topic = built["children"][0]["children"][0]
    assert app.tree_edit({"action": "rename", "node": topic["id"], "name": "Renamed"})["tree"]
    with pytest.raises(ValueError):
        app.tree_edit({"action": "move", "node": "nope", "family": "g0"})

    saved = views.save_view(conn, "Tree", {"tab": "tree", "tree_path": ["g0", topic["id"], 7]})
    assert saved["state"]["tab"] == "tree" and saved["state"]["tree_path"] == ["g0", topic["id"]]
    assert views.save_view(conn, "Grid", {"tab": "grid"})["state"]["tab"] == "grid"

    # A snapshot of a few bookmarks keeps the grid and the tree, cut down to them.
    keep = [b for cell in cells for b in cell["ids"]][:10]
    target = tmp_path / ("part" + snapshot.SUFFIX)
    snapshot.export_snapshot(conn, target, ids=keep)
    received = db.open_ready(target)
    assert sum(cell["size"] for cell in store.grid_data(received)["cells"]) == 10
    shared = store.tree_data(received)
    assert shared["tree"]["size"] == 10 and shared["missing"] == 0
    assert tree.find(shared["tree"], topic["id"])[0]["name"] == "Renamed"


def test_groups_get_names_that_tell_them_apart():
    tags = {}
    groups = {}
    # Three groups all tagged "Open Source"; each also has something of its own.
    for key, own, size in (("a", "Rust", 12), ("b", "Blender", 8), ("c", "Audio", 5)):
        groups[key] = [f"{key}{i}" for i in range(size)]
        for i, bookmark in enumerate(groups[key]):
            tags[bookmark] = ["Open Source"] + ([own] if i % 2 == 0 else [])
    groups["d"] = ["d0", "d1", "d2"]
    for bookmark in groups["d"]:
        tags[bookmark] = ["Open Source"]  # nothing of its own
    groups["e"] = []
    groups["f"] = ["f0"]
    tags["f0"] = []
    names = tree.distinct_names(groups, tags)
    assert [names[k][0] for k in "abc"] == ["Rust", "Blender", "Audio"]
    assert names["a"][1:] == ["Open Source"]
    assert names["d"][0] == "Open Source"  # the only thing it has; free, since nobody took it
    assert names["e"] == [] and names["f"] == []
    firsts = [value[0] for value in names.values() if value]
    assert len(firsts) == len(set(firsts))
    # Two groups with the very same tags cannot both be told apart by one tag.
    twins = {"x": ["x0", "x1"], "y": ["y0", "y1"]}
    same = {b: ["LLM", "Agents"] for ids in twins.values() for b in ids}
    named = tree.distinct_names(twins, same)
    assert named["x"][0] != named["y"][0] and {named["x"][0], named["y"][0]} == {"LLM", "Agents"}
    # A tag carried by a single bookmark of a large group does not name it.
    big = {"g": [f"g{i}" for i in range(10)], "h": ["h0", "h1"]}
    mixed = {b: ["Common"] for b in big["g"] + big["h"]}
    mixed["g0"] = ["Common", "Oddity"]
    assert tree.distinct_names(big, mixed)["g"][0] == "Common"
