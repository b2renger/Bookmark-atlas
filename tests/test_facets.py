import pytest

from atlas import db, views
from atlas.exporter import facet_options, matching_ids
from atlas.server import App
from test_clusters import path  # noqa: F401
from test_links_trends import add


@pytest.fixture
def conn(path):  # noqa: F811
    conn = db.open_ready(path)
    add(
        conn,
        "https://x.com/a/status/1",
        "one",
        "splats",
        ["Splats", "3D"],
        saved="2026-03-10T09:00:00Z",
        published="2026-03-01",
    )
    add(conn, "https://x.com/a/status/2", "two", "splats", ["Splats"], published="2026-01-15")
    add(
        conn,
        "https://github.com/a/b",
        "three",
        "audio",
        ["Audio", "3D"],
        saved="2025-06-01T00:00:00Z",
    )
    add(conn, "https://e.org/four", "four", "nothing", [])
    conn.execute("UPDATE bookmarks SET platform = 'X' WHERE url LIKE 'https://x.com%'")
    conn.execute("UPDATE bookmarks SET platform = 'GitHub' WHERE url LIKE 'https://github%'")
    conn.commit()
    return conn


def titles(conn, **facets):
    ids = matching_ids(conn, facets.pop("q", ""), None, facets)
    marks = ",".join("?" * len(ids))
    return sorted(
        r[0] for r in conn.execute(f"SELECT title FROM bookmarks WHERE id IN ({marks})", ids)
    )


def test_tags_must_all_match_and_ignore_case(conn):
    assert titles(conn, tags=["splats"]) == ["one", "two"]
    assert titles(conn, tags=["3D"]) == ["one", "three"]
    assert titles(conn, tags=["Splats", "3d"]) == ["one"]
    assert titles(conn, tags=["Spl"]) == []  # a whole tag, not a part of one
    assert titles(conn, tags=["", "  "]) == ["four", "one", "three", "two"]


def test_platform_and_search_combine_with_tags(conn):
    assert titles(conn, platform="x") == ["one", "two"]
    assert titles(conn, platform="X", tags=["3D"]) == ["one"]
    assert titles(conn, q="audio", platform="GitHub") == ["three"]
    assert titles(conn, q="audio", platform="X") == []


def test_date_range_on_each_kind_of_date(conn):
    # "when": the saved day, or the published day when there is no saved day.
    assert titles(conn, date_from="2026-01-01") == ["one", "two"]
    assert titles(conn, date_from="2026-03-10", date_to="2026-03-10") == ["one"]
    assert titles(conn, date_to="2025-12-31") == ["three"]
    assert titles(conn, date_from="2026-01-01", date_kind="saved") == ["one"]
    assert titles(conn, date_from="2026-01-01", date_to="2026-03-05", date_kind="published") == [
        "one",
        "two",
    ]
    # An undated bookmark never passes a date filter.
    assert "four" not in titles(conn, date_from="1990-01-01")
    for bad in (
        {"date_from": "March"},
        {"date_to": "2026-13-01"},
        {"date_from": "2026-01-01", "date_kind": "x"},
    ):
        with pytest.raises(ValueError):
            titles(conn, **bad)


def test_filter_text_is_never_sql(conn):
    assert titles(conn, tags=["x' OR 1=1 --"]) == []
    assert titles(conn, platform="X' OR '1'='1") == []


def test_options_offered_by_the_filter_bar(conn):
    options = facet_options(conn)
    assert [(t["name"], t["count"]) for t in options["tags"]] == [
        ("3D", 2),
        ("Splats", 2),
        ("Audio", 1),
    ]
    assert [(p["name"], p["count"]) for p in options["platforms"]][:2] == [("X", 2), ("GitHub", 1)]
    assert (options["first_day"], options["last_day"]) == ("2025-06-01", "2026-03-10")
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE title = 'three'")
    conn.commit()
    assert [t["name"] for t in facet_options(conn)["tags"]] == ["Splats", "3D"]


def test_filter_through_the_api(conn, path):  # noqa: F811
    app = App(path)
    assert len(app.bookmark_ids({"tag": ["Splats"], "platform": ["X"]})["ids"]) == 2
    listed = app.bookmarks({"tag": ["3D"], "from": ["2026-01-01"], "dates": ["when"]})
    assert listed["total"] == 1 and listed["items"][0]["title"] == "one"
    everything = app.bookmark_ids({})["ids"]
    only = app.bookmarks_listed(
        {"ids": everything, "facets": {"tags": ["3D"], "platform": "GitHub"}}
    )
    assert [item["title"] for item in only["items"]] == ["three"]
    assert app.bookmarks_listed({"ids": everything, "facets": "nonsense"})["total"] == 4
    assert app.facets()["tags"][0]["count"] == 2
    with pytest.raises(ValueError):
        app.bookmark_ids({"from": ["soon"]})


def test_a_view_remembers_the_filter_and_the_arrangement(conn):
    state = views.save_view(
        conn,
        "v",
        {
            "tab": "map",
            "facets": {
                "tags": ["Splats", 7, " "],
                "platform": "X",
                "date_from": "2026-01-01x",
                "date_kind": "nope",
                "more": 1,
            },
            "map": {"layout": "spacetime"},
        },
    )["state"]
    assert state["facets"] == {
        "tags": ["Splats"],
        "platform": "X",
        "date_from": "2026-01-01",
        "date_to": "",
        "date_kind": "when",
    }
    assert state["map"]["layout"] == "spacetime"
    assert (
        views.save_view(conn, "w", {"map": {"layout": "sideways"}})["state"]["map"]["layout"]
        == "similarity"
    )
