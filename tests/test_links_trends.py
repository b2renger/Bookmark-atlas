import pytest

np = pytest.importorskip("numpy")

from atlas import db, trends  # noqa: E402
from atlas.analysis import links, store  # noqa: E402
from atlas.importer import ImportReport, import_record  # noqa: E402
from atlas.server import App  # noqa: E402
from test_clusters import fill, path  # noqa: E402, F401


def add(conn, url, title, summary, tags, saved=None, published=None):
    import_record(
        conn,
        ImportReport(),
        source="csv",
        raw={},
        url=url,
        title=title,
        summary=summary,
        tags=tags,
        date_text=None,
    )
    conn.execute(
        "UPDATE bookmarks SET saved_at = ?, published_at = ? WHERE url = ?", (saved, published, url)
    )
    conn.commit()
    return conn.execute("SELECT id FROM bookmarks WHERE url = ?", (url,)).fetchone()[0]


def test_related_bookmarks_come_from_the_same_topic(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    one = conn.execute("SELECT id FROM bookmarks WHERE url LIKE '%/audio/3'").fetchone()[0]
    found = links.related(conn, one, limit=5)
    assert len(found["related"]) == 5 and all(r["id"] != one for r in found["related"])
    assert all("Audio" in r["shared_tags"] and r["same_site"] for r in found["related"])
    scores = [r["similarity"] for r in found["related"]]
    assert scores == sorted(scores, reverse=True)
    with pytest.raises(ValueError):
        links.related(conn, "nope")


def test_overview_without_and_with_a_clustering(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    assert links.overview(conn)["topics"] == []
    run = store.cluster_run(conn, 3)
    data = links.overview(conn)
    assert data["cluster_run"] == run["run_id"]
    assert sorted(t["size"] for t in data["topics"]) == [24, 24, 24]
    # Three separate topics: few links cross, and nothing is a bridge.
    assert sum(link["links"] for link in data["links"]) < 20 and data["bridges"] == []
    assert 1 <= len(data["outliers"]) <= 15
    for link in data["links"]:
        assert link["a"] < link["b"] and link["strength"] > 0


def test_a_bookmark_between_two_topics_is_a_bridge(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    run = store.cluster_run(conn, 3)
    # File an audio bookmark under the splats topic by hand.
    audio = conn.execute("SELECT id FROM bookmarks WHERE url LIKE '%/audio/5'").fetchone()[0]
    splat = conn.execute("SELECT id FROM bookmarks WHERE url LIKE '%/splats/5'").fetchone()[0]
    number = lambda b: conn.execute(  # noqa: E731
        "SELECT cluster FROM run_items WHERE run_id = ? AND bookmark_id = ?", (run["run_id"], b)
    ).fetchone()[0]
    home, away = number(audio), number(splat)
    conn.execute(
        "UPDATE run_items SET cluster = ? WHERE run_id = ? AND bookmark_id = ?",
        (away, run["run_id"], audio),
    )
    conn.commit()
    bridges = links.overview(conn)["bridges"]
    assert [(b["id"], b["topic"], b["towards"], b["share"]) for b in bridges] == [
        (audio, away, home, 1.0)
    ]


def test_old_bookmarks_close_to_recent_ones_resurface(path):  # noqa: F811
    conn = db.open_ready(path)
    assert links.resurface.__defaults__ == (45, 180, 15)
    fill(conn)  # undated: they can be neither recent nor old
    old = add(
        conn,
        "https://e.org/old",
        "splatting capture",
        "gaussian splatting radiance field scene capture",
        ["Splats"],
        saved="2025-01-10T10:00:00Z",
    )
    add(
        conn,
        "https://e.org/old2",
        "synth",
        "music synthesizer audio sound",
        ["Audio"],
        published="2024-06-01",
    )
    new = add(
        conn,
        "https://e.org/new",
        "capture splatting",
        "gaussian splatting radiance field scene capture",
        ["Splats"],
        saved="2026-03-01T10:00:00Z",
    )
    data = links.resurface(conn)
    assert (data["latest"], data["recent"], data["old"]) == ("2026-03-01", 1, 2)
    assert [(i["id"], i["echoes"], i["day"]) for i in data["items"]] == [(old, 1, "2025-01-10")]
    assert data["items"][0]["because"][0]["id"] == new
    conn.execute("UPDATE bookmarks SET saved_at = NULL, published_at = NULL")
    conn.commit()
    assert links.resurface(conn)["items"] == [] and links.resurface(conn)["latest"] is None


def test_trends_count_months_gaps_and_movement(path):  # noqa: F811
    conn = db.open_ready(path)
    empty = trends.overview(conn)
    assert empty["latest"] is None and empty["coverage"]["bookmarks"] == 0
    for i in range(6):  # six old splat bookmarks, saved a week after publication
        add(
            conn,
            f"https://e.org/a{i}",
            f"a{i}",
            "x",
            ["Splats"],
            saved=f"2025-0{i + 1}-10T08:00:00Z",
            published=f"2025-0{i + 1}-03",
        )
    for i in range(5):  # five recent audio bookmarks, published only
        add(conn, f"https://e.org/b{i}", f"b{i}", "x", ["Audio"], published=f"2026-02-{10 + i:02d}")
    add(
        conn,
        "https://e.org/c",
        "c",
        "x",
        ["Audio"],
        saved="2026-02-20T08:00:00Z",
        published="2026-02-20",
    )
    add(conn, "https://e.org/d", "d", "x", [], saved="2024-01-01T00:00:00Z", published="2024-03-01")
    add(conn, "https://e.org/e", "e", "x", [])

    data = trends.overview(conn)
    assert data["coverage"] == {
        "bookmarks": 14,
        "saved": 8,
        "published": 13,
        "both": 8,
        "neither": 1,
    }
    assert data["latest"] == "2026-02-20"
    months = {m["month"]: m["count"] for m in data["saved"]["months"]}
    assert list(months)[0] == "2024-01" and list(months)[-1] == "2026-02"
    assert len(months) == 26 and months["2025-03"] == 1 and months["2025-08"] == 0
    assert {m["month"]: m["count"] for m in data["published"]["months"]}["2026-02"] == 6
    assert data["reaction"]["measured"] == 7 and data["reaction"]["saved_before_published"] == 1
    assert data["reaction"]["median_days"] == 7
    assert [b["count"] for b in data["reaction"]["buckets"]] == [1, 6, 0, 0, 0]
    assert (data["window"]["recent"], data["window"]["before"]) == (6, 7)
    assert data["window"]["by_published_date"] == 5
    assert [m["name"] for m in data["tags"]["rising"]] == ["Audio"]
    assert [m["name"] for m in data["tags"]["fading"]] == ["Splats"]
    assert data["tags"]["rising"][0]["share_recent"] == 1.0
    assert data["topics"] is None

    # With a clustering, topics move too, under the names given.
    run_id = "r1"
    conn.execute(
        "INSERT INTO runs (id, created_at, kind, algorithm, params, app_version) "
        "VALUES (?, '2026-01-01', 'cluster', 'test', '{}', 'x')",
        (run_id,),
    )
    for row in conn.execute("SELECT id, url FROM bookmarks").fetchall():
        conn.execute(
            "INSERT INTO run_items (run_id, bookmark_id, cluster) VALUES (?, ?, ?)",
            (run_id, row["id"], 0 if "/a" in row["url"] else 1),
        )
    moves = trends.overview(conn, run_id, {0: "Old things"})["topics"]["moves"]
    assert [(m["name"], m["recent"], m["before"]) for m in moves] == [
        ("Topic 1", 6, 1),
        ("Old things", 0, 6),
    ]
    assert moves[0]["change"] == -moves[1]["change"] > 0


def test_only_the_last_months_are_shown(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://e.org/1", "1", "x", [], published="2015-05-05")
    add(conn, "https://e.org/2", "2", "x", [], published="2026-05-05")
    data = trends.overview(conn)["published"]
    assert len(data["months"]) == trends.MONTHS_SHOWN and data["months"][0]["month"] == "2023-06"
    assert data["earlier"] == 1 and data["total"] == 2


def test_links_and_trends_through_the_api(path):  # noqa: F811
    conn = db.open_ready(path)
    fill(conn)
    app = App(path)
    assert app.links_show()["topics"] == [] and app.trends_show()["latest"] is None
    store.cluster_run(conn, 3)
    shown = app.links_show()
    assert len(shown["topics"]) == 3 and shown["resurface"]["items"] == []
    one = conn.execute("SELECT id FROM bookmarks LIMIT 1").fetchone()[0]
    assert len(app.links_related({"id": [one]})["related"]) == 8
    with pytest.raises(ValueError):
        app.links_related({})
    conn.execute("UPDATE bookmarks SET saved_at = '2026-01-05T00:00:00Z'")
    conn.commit()
    topics = app.trends_show()["topics"]
    assert len(topics["moves"]) == 3 and all(
        " · " in m["name"] or m["name"] for m in topics["moves"]
    )
