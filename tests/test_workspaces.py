import pytest

from atlas import db, groom, importer, workspaces
from atlas.server import App
from test_clusters import path  # noqa: F401
from test_links_trends import add

BROWSER_FILE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1700000000" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>
    <DL><p>
        <DT><A HREF="https://example.org/one" ADD_DATE="1767268800">First &amp; foremost</A>
        <DT><H3>Reading list</H3>
        <DL><p>
            <DT><A HREF="https://example.org/two" ADD_DATE="1767355200">Second</A>
            <DT><H3>Deep</H3>
            <DL><p>
                <DT><A HREF="https://example.org/three">Third, no date</A>
            </DL><p>
            <DT><A HREF="javascript:void(0)">A bookmarklet</A>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://example.org/four" ADD_DATE="not a number">Outside every folder</A>
</DL><p>
"""


def titles(conn):
    return sorted(
        r[0] for r in conn.execute("SELECT title FROM bookmarks WHERE deleted_at IS NULL")
    )


def tags(conn, title):
    return sorted(
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id "
            "JOIN bookmarks b ON b.id = bt.bookmark_id WHERE b.title = ?",
            (title,),
        )
    )


# ---- browser bookmarks -------------------------------------------------------


def test_a_browser_file_is_read_with_its_folders_and_dates():
    assert importer.looks_like_browser_export(BROWSER_FILE)
    assert not importer.looks_like_browser_export("Name,URL\\nx,https://e.org")
    items = importer.read_browser_bookmarks(BROWSER_FILE)
    assert [(i["title"], i["folders"]) for i in items] == [
        ("First & foremost", []),  # the browser's own top folder is not a tag
        ("Second", ["Reading list"]),
        ("Third, no date", ["Reading list", "Deep"]),
        ("A bookmarklet", ["Reading list"]),
        ("Outside every folder", []),
    ]


def test_browser_bookmarks_become_bookmarks_with_tags_and_save_dates(path):  # noqa: F811
    conn = db.open_ready(path)
    assert importer.import_browser_bookmarks(conn, BROWSER_FILE, dry_run=True).created == 4
    assert titles(conn) == []
    report = importer.import_browser_bookmarks(conn, BROWSER_FILE)
    assert report.created == 4 and len(report.skipped) == 1  # the bookmarklet is not a web link
    assert titles(conn) == ["First & foremost", "Outside every folder", "Second", "Third, no date"]
    assert tags(conn, "Third, no date") == ["Deep", "Reading list"]
    saved = dict(conn.execute("SELECT title, saved_at FROM bookmarks"))
    assert saved["First & foremost"] == "2026-01-01T12:00:00Z" and saved["Third, no date"] is None
    assert saved["Outside every folder"] is None
    assert importer.import_browser_bookmarks(conn, BROWSER_FILE).created == 0  # nothing twice
    app = App(path)
    again = app.import_csv({"content": BROWSER_FILE, "filename": "bookmarks.html", "dry_run": True})
    assert again["report"]["rows"] == 5 and again["report"]["created"] == 0


# ---- workspaces --------------------------------------------------------------


@pytest.fixture
def home(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://e.org/a", "a", "s", ["Splats"])
    add(conn, "https://e.org/b", "b", "s", ["Splats"])
    add(conn, "https://e.org/c", "c", "s", ["Audio"])
    return path


def test_a_workspace_is_a_copy_that_lives_apart(home):
    conn = db.open_ready(home)
    ids = [r[0] for r in conn.execute("SELECT id FROM bookmarks WHERE title IN ('a', 'b')")]
    made = workspaces.create(conn, home, "  Splat   study ", ids=ids, started_from="the selection")
    assert made["id"] == "splat-study" and made["name"] == "Splat study" and made["bookmarks"] == 2
    target = workspaces.path_of(home, "splat-study")
    assert target.parent.parent == home.parent / "Workspaces"
    space = db.open_ready(target)
    assert titles(space) == ["a", "b"] and workspaces.info(space)["started_from"] == "the selection"
    # It is not presented as a snapshot.
    assert space.execute("SELECT COUNT(*) FROM meta WHERE key = 'snapshot'").fetchone()[0] == 0
    # Changes in the workspace do not reach the main collection.
    space.execute("UPDATE bookmarks SET title = 'a, renamed' WHERE title = 'a'")
    space.commit()
    assert titles(conn) == ["a", "b", "c"]

    assert workspaces.create(conn, home, "Splat study", empty=True)["id"] == "splat-study-2"
    assert workspaces.create(conn, home, "Everything")["bookmarks"] == 3
    listed = workspaces.list_all(home)
    assert sorted(w["id"] for w in listed) == ["everything", "splat-study", "splat-study-2"]
    assert {w["id"]: w["bookmarks"] for w in listed}["splat-study-2"] == 0

    workspaces.delete(home, "splat-study-2")
    assert not (home.parent / "Workspaces" / "splat-study-2").exists()
    for bad in ("", "..", "../..", "splat-study/..", "nope", "Splat Study", "a/b"):
        with pytest.raises(ValueError):
            workspaces.path_of(home, bad)
        with pytest.raises(ValueError):
            workspaces.delete(home, bad)
    with pytest.raises(ValueError):
        workspaces.create(conn, home, "   ")
    with pytest.raises(ValueError):
        workspaces.create(conn, home, "Nothing", ids=["no-such-id"])
    assert not (home.parent / "Workspaces" / "nothing").exists()  # a failed creation leaves nothing


def test_merging_a_workspace_only_adds(home):
    conn = db.open_ready(home)
    workspaces.create(conn, home, "Study")
    space = db.open_ready(workspaces.path_of(home, "study"))
    add(space, "https://e.org/new", "new in the workspace", "s", ["Fresh"])
    add(space, "https://e.org/gone", "deleted at home", "s", [])
    importer.import_browser_bookmarks(space, BROWSER_FILE)
    b = space.execute("SELECT id FROM bookmarks WHERE title = 'b'").fetchone()[0]
    tag = space.execute("INSERT INTO tags (name) VALUES ('Worth keeping')").lastrowid
    space.execute("INSERT INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)", (b, tag))
    space.execute(
        "UPDATE bookmarks SET deleted_at = 'x' WHERE title = 'c'"
    )  # deleting there deletes nothing here
    space.commit()
    add(conn, "https://e.org/gone", "deleted at home", "s", [])
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE title = 'deleted at home'")
    conn.commit()

    result = workspaces.merge(conn, home, "study")
    assert result == {"added": 5, "tagged": 1, "kept_deleted": 1, "already_there": 2}
    assert titles(conn) == [
        "First & foremost",
        "Outside every folder",
        "Second",
        "Third, no date",
        "a",
        "b",
        "c",
        "new in the workspace",
    ]
    assert tags(conn, "b") == ["Splats", "Worth keeping"] and tags(
        conn, "new in the workspace"
    ) == ["Fresh"]
    # The tag run can be undone from the grooming history.
    run = groom.history(conn)[0]
    assert "merge workspace" in run["script"]
    groom.undo(conn, run["run_id"], db_path=home)
    assert tags(conn, "b") == ["Splats"]
    # Merging again adds nothing new.
    assert workspaces.merge(conn, home, "study", add_tags=False)["added"] == 0


def test_workspaces_through_the_api(home):
    app = App(home)
    assert app.workspaces_list() == {
        "home": {"bookmarks": 3},
        "active": None,
        "workspaces": [],
        "folder": str(home.parent / "Workspaces"),
    }
    assert app.state()["workspace"] is None
    ids = app.bookmark_ids({"tag": ["Splats"]})["ids"]
    made = app.workspace_create(
        {"name": "Splats only", "start": "ids", "ids": ids, "started_from": "tag Splats"}
    )
    assert made["active"] == "splats-only" and made["created"]["bookmarks"] == 2
    # The app now works in the workspace.
    assert (
        app.state()["workspace"]["name"] == "Splats only"
        and app.state()["status"]["bookmarks"] == 2
    )
    app.import_csv({"content": BROWSER_FILE, "filename": "bookmarks.html"})
    assert app.state()["status"]["bookmarks"] == 6
    assert app.workspaces_list()["home"]["bookmarks"] == 3  # the main collection did not move

    assert (
        app.workspace_open({"id": "home"})["active"] is None
        and app.state()["status"]["bookmarks"] == 3
    )
    assert app.workspace_open({"id": "splats-only"})["active"] == "splats-only"
    merged = app.workspace_merge({"id": "splats-only"})
    assert merged["merged"]["added"] == 4 and merged["home"]["bookmarks"] == 7
    assert (
        app.workspace_delete({"id": "splats-only"})["active"] is None
    )  # deleting the open one goes home
    assert app.state()["workspace"] is None and app.workspaces_list()["workspaces"] == []
    empty = app.workspace_create({"name": "Blank"})
    assert empty["created"]["bookmarks"] == 0 and app.state()["status"]["bookmarks"] == 0
    for bad in ({"name": ""}, {"name": "x", "start": "ids", "ids": []}):
        with pytest.raises(ValueError):
            app.workspace_create(bad)
    with pytest.raises(ValueError):
        app.workspace_open({"id": "../.."})
