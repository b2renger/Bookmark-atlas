import json

import pytest

np = pytest.importorskip("numpy")

from atlas import db  # noqa: E402
from atlas.analysis import store  # noqa: E402
from atlas.cli import main  # noqa: E402
from atlas.importer import ImportReport, import_record  # noqa: E402

TOPICS = {
    "Splats": "gaussian splatting radiance field reconstruction scene capture photogrammetry",
    "Language models": "language model prompt agent token inference chat local",
    "Audio": "music synthesizer audio sound sample track mixing",
}


def fill(conn, per_topic=24, seed=0):
    rng = np.random.default_rng(seed)
    report = ImportReport()
    for tag, words in TOPICS.items():
        vocabulary = words.split()
        for index in range(per_topic):
            chosen = list(rng.choice(vocabulary, 5))
            import_record(
                conn,
                report,
                source="csv",
                raw={},
                url=f"https://example.org/{tag.lower().replace(' ', '-')}/{index}",
                title=f"{chosen[0]} {chosen[1]} {index}",
                summary=" ".join(chosen) + " tool",
                tags=[tag, "Tool"],
                date_text=None,
            )
    conn.commit()


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "atlas.sqlite"
    main(["--db", str(target), "init"])
    return target


@pytest.fixture
def conn(path):
    connection = db.open_ready(path)
    fill(connection)
    yield connection
    connection.close()


def test_vectors_are_built_stored_and_refreshed_when_text_changes(conn):
    assert not store.vectors_are_current(conn)
    ids, matrix, model = store.load_vectors(conn)
    assert len(ids) == 72 and matrix.shape[0] == 72 and model.startswith("lsa-")
    assert store.vectors_are_current(conn)

    conn.execute("UPDATE bookmarks SET title = 'entirely new words here' WHERE id = ?", (ids[0],))
    conn.commit()
    assert not store.vectors_are_current(conn)
    store.load_vectors(conn)
    assert store.vectors_are_current(conn)


def test_deleted_bookmarks_are_left_out(conn):
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE url LIKE '%/audio/%'")
    conn.commit()
    ids, matrix, _ = store.load_vectors(conn)
    assert len(ids) == 48
    run = store.cluster_run(conn, 2)
    assert sum(c["size"] for c in run["clusters"]) == 48


def test_empty_collection_is_explained(path):
    conn = db.open_ready(path)
    with pytest.raises(ValueError, match="no bookmarks to analyse"):
        store.cluster_run(conn, 3)
    conn.close()


@pytest.mark.parametrize("algorithm", ["k-means", "e-means", "e-means++"])
def test_a_run_finds_the_three_topics_and_describes_them(conn, algorithm):
    run = store.cluster_run(conn, 3, algorithm=algorithm, seed=1)

    assert run["algorithm"] == algorithm and run["params"]["k"] == 3
    assert sum(c["size"] for c in run["clusters"]) == 72
    if algorithm != "e-means":
        assert sorted(c["size"] for c in run["clusters"]) == [24, 24, 24]
        # The telling tag of each cluster is its topic, not the tag every bookmark has.
        assert {c["top_tags"][0]["name"] for c in run["clusters"]} == set(TOPICS)
        assert all(len(c["samples"]) == 5 for c in run["clusters"])
        assert run["metrics"]["silhouette"] > 0.2
    # Plain e-means starts from random points and, on a set this small, can
    # merge two topics (docs/reports/e-means-test.md). Only its bookkeeping is
    # checked here.
    stored = conn.execute(
        "SELECT COUNT(*) FROM run_items WHERE run_id = ?", (run["run_id"],)
    ).fetchone()[0]
    assert stored == 72


def test_auto_picks_the_number_of_clusters(conn):
    run = store.cluster_run(conn)
    assert run["params"]["k"] in (2, 3, 4, 6) and "k_suggested_from" in run["params"]
    assert store.suggest_k(conn)["suggested"] == run["params"]["k"]


def test_runs_are_listed_labelled_and_deleted(conn):
    first = store.cluster_run(conn, 3)
    second = store.cluster_run(conn, 4, algorithm="e-means")
    assert [r["run_id"] for r in store.list_runs(conn)] == [second["run_id"], first["run_id"]]
    assert store.latest_run_id(conn) == second["run_id"]

    number = first["clusters"][0]["cluster"]
    store.set_cluster_label(conn, first["run_id"], number, "  My   topic ")
    shown = next(
        c for c in store.describe_run(conn, first["run_id"])["clusters"] if c["cluster"] == number
    )
    assert shown["label"] == "My topic" and shown["edited"]
    store.set_cluster_label(conn, first["run_id"], number, "")
    shown = next(
        c for c in store.describe_run(conn, first["run_id"])["clusters"] if c["cluster"] == number
    )
    assert not shown["edited"] and shown["label"] in TOPICS or " · " in shown["label"]

    store.delete_run(conn, first["run_id"])
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id = ?", (first["run_id"],)
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM cluster_edits").fetchone()[0] == 0
    with pytest.raises(ValueError, match="No such run"):
        store.delete_run(conn, first["run_id"])


def test_compare_reports_every_algorithm_and_setting(conn):
    data = store.compare(conn, [3, 4], seeds=3)
    assert data["bookmarks"] == 72 and len(data["results"]) == 6
    three = {r["algorithm"]: r for r in data["results"] if r["k"] == 3}
    assert three["k-means"]["stability"] == pytest.approx(1.0)
    assert three["k-means"]["agreement_with_kmeans"] is None
    assert 0 <= three["e-means"]["agreement_with_kmeans"] <= 1
    table = store.comparison_markdown(data)
    assert table.count("\n| 3 |") == 3 and "silhouette" in table


def test_cluster_filter_on_bookmarks(conn):
    from atlas.exporter import matching_ids

    run = store.cluster_run(conn, 3)
    item = run["clusters"][0]
    ids = matching_ids(conn, None, f"{run['run_id']}:{item['cluster']}")
    assert len(ids) == item["size"]
    assert (
        len(matching_ids(conn, "no such word anywhere", f"{run['run_id']}:{item['cluster']}")) == 0
    )
    with pytest.raises(ValueError, match="cluster filter"):
        matching_ids(conn, None, "broken")


def test_cli_embed_cluster_compare(path, capsys, tmp_path):
    conn = db.open_ready(path)
    fill(conn)
    conn.close()
    assert main(["--db", str(path), "embed"]) == 0
    assert "Built 72 vectors" in capsys.readouterr().out
    assert main(["--db", str(path), "cluster", "--k", "3", "--algo", "e-means++"]) == 0
    out = capsys.readouterr().out
    assert "e-means++, 3 clusters" in out and "  24  " in out
    report = tmp_path / "report.md"
    assert main(["--db", str(path), "compare", "--k", "3", "--seeds", "2", "-o", str(report)]) == 0
    assert "| 3 | e-means |" in report.read_text(encoding="utf-8")
    assert main(["--db", str(path), "cluster", "--k", "500"]) == 1
    assert "between 2 and 72" in capsys.readouterr().err


def test_clusters_through_the_api(tmp_path, monkeypatch):
    import threading
    import urllib.request

    from atlas.server import App, make_server

    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    conn = db.open_ready(path)
    fill(conn)
    conn.close()
    app = App(path)
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Atlas-Token": app.session_token, "Content-Type": "application/json"}
        request = urllib.request.Request(base + route, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert call("GET", "/api/clusters/run")[1] == {"run": None}
        code, answer = call("POST", "/api/clusters/run", {"k": "3", "algorithm": "e-means"})
        assert code == 200 and len(answer["run"]["clusters"]) == 3
        run_id = answer["run"]["run_id"]
        number = answer["run"]["clusters"][0]["cluster"]
        assert call("GET", "/api/clusters/runs")[1]["runs"][0]["run_id"] == run_id
        assert call("GET", f"/api/bookmarks?cluster={run_id}:{number}")[1]["total"] == 24
        assert len(call("GET", f"/api/bookmarks/ids?cluster={run_id}:{number}")[1]["ids"]) == 24
        code, answer = call(
            "POST", "/api/clusters/label", {"run_id": run_id, "cluster": number, "label": "Mine"}
        )
        assert code == 200 and any(c["label"] == "Mine" for c in answer["run"]["clusters"])
        assert call("POST", "/api/clusters/run", {"k": "many"})[0] == 400
        assert call("POST", "/api/clusters/run", {"k": 3, "algorithm": "magic"})[0] == 400
        assert call("POST", "/api/clusters/delete", {"run_id": run_id})[1] == {"runs": []}
    finally:
        server.shutdown()
        server.server_close()
