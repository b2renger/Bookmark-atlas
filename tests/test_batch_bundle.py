import json
from pathlib import Path

import pytest

from atlas import batch, bundle, db, groom
from atlas.cli import main
from atlas.importer import import_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


def make_db(tmp_path, name, filled=True):
    path = tmp_path / name / "atlas.sqlite"
    path.parent.mkdir()
    main(["--db", str(path), "init"])
    conn = db.open_ready(path)
    if filled:
        import_csv(conn, FIXTURE)
    return path, conn


@pytest.fixture
def filled(tmp_path):
    path, conn = make_db(tmp_path, "a")
    yield path, conn
    conn.close()


def ids_of(conn, *titles):
    return [
        conn.execute("SELECT id FROM bookmarks WHERE title = ?", (t,)).fetchone()[0] for t in titles
    ]


def tags(conn, title):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id "
            "JOIN bookmarks b ON b.id = bookmark_id WHERE b.title = ?",
            (title,),
        )
    }


# ---- batch edits -----------------------------------------------------------


def test_add_a_tag_to_several_bookmarks_and_undo(filled):
    path, conn = filled
    ids = ids_of(conn, "Lantern talk", "lantern-core", "Paper Lantern Renderer")

    result = batch.apply_batch(conn, ids, add_tags=["Open Source"], db_path=path)

    # Two of the three already had it: only the missing one changes.
    assert result["applied"] == 1 and result["groups"] == 1
    assert "Open Source" in tags(conn, "Lantern talk")
    assert groom.history(conn)[0]["script"] == "edit-tags"

    groom.undo(conn, result["run_id"])
    assert "Open Source" not in tags(conn, "Lantern talk")


def test_remove_and_add_in_one_run(filled):
    path, conn = filled
    ids = ids_of(conn, "Lantern talk", "Lantern thread")
    result = batch.apply_batch(conn, ids, add_tags=["  To   read "], remove_tags=["Rendering"])
    assert result["applied"] == 4
    assert tags(conn, "Lantern talk") == {"Talk", "To read"}
    assert tags(conn, "Lantern thread") == {"Announcement", "To read"}


def test_adding_a_merged_spelling_adds_the_kept_one(filled):
    path, conn = filled
    groom.apply(conn, groom.get_script("merge-duplicates"))
    groom.apply(conn, groom.get_script("normalize-tags"))  # "rendering" -> "Rendering"
    batch.apply_batch(conn, ids_of(conn, "Undated note"), add_tags=["rendering"])
    assert tags(conn, "Undated note") == {"Misc", "Rendering"}


def test_delete_selection_is_soft_and_undoable(filled):
    path, conn = filled
    result = batch.apply_batch(conn, ids_of(conn, "Undated note", "Lantern talk"), delete=True)
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL").fetchone()[0] == 3
    )
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 5
    groom.undo(conn, result["run_id"])
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL").fetchone()[0] == 5
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"add_tags": []}, "Nothing to do"),
        ({"add_tags": ["a, b"]}, "comma"),
        ({"add_tags": ["x" * 81]}, "limited to"),
        ({"add_tags": ["A"], "remove_tags": ["A"]}, "both added and removed"),
        ({"add_tags": [3]}, "must be text"),
    ],
)
def test_bad_batch_requests_are_explained(filled, kwargs, message):
    path, conn = filled
    with pytest.raises(ValueError, match=message):
        batch.apply_batch(conn, ids_of(conn, "Lantern talk"), **kwargs)
    with pytest.raises(ValueError, match="Select at least one"):
        batch.apply_batch(conn, [], add_tags=["A"])


def test_unknown_and_deleted_ids_are_ignored(filled):
    path, conn = filled
    conn.execute("UPDATE bookmarks SET deleted_at = 'x' WHERE title = 'Lantern talk'")
    conn.commit()
    ids = ids_of(conn, "Lantern talk", "Undated note") + ["no-such-id"]
    assert batch.apply_batch(conn, ids, add_tags=["New"])["groups"] == 1
    assert [t["name"] for t in batch.tag_counts(conn, ids)] == ["Misc", "New"]


# ---- bundles ---------------------------------------------------------------


def test_bundle_holds_curated_fields_only(filled):
    path, conn = filled
    groom.apply(conn, groom.get_script("derive-platform"))
    data = bundle.export_bundle(conn, ids_of(conn, "lantern-core", "Lantern talk"))

    assert data["format"] == "bookmark-atlas-bundle" and data["version"] == 1 and data["count"] == 2
    item = next(i for i in data["bookmarks"] if i["title"] == "lantern-core")
    assert item == {
        "url": "https://github.com/Example-Org/Lantern-Core",
        "title": "lantern-core",
        "summary": "Fictional repository holding the renderer core.",
        "tags": ["C++", "Open Source"],
        "platform": "GitHub",
        "published_at": None,
        "published_source": None,
    }
    text = json.dumps(data)
    assert "Platform" not in text and "raw" not in text and "saved_at" not in text


def test_a_subset_travels_to_another_user(filled, tmp_path):
    path, conn = filled
    for name in ("derive-platform", "published-from-x-id"):
        groom.apply(conn, groom.get_script(name))
    data = json.loads(
        json.dumps(bundle.export_bundle(conn, ids_of(conn, "Lantern thread", "lantern-core")))
    )

    other_path, other = make_db(tmp_path, "b", filled=False)
    preview = bundle.import_bundle(other, data, dry_run=True)
    assert preview.created == 2
    assert other.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 0

    report = bundle.import_bundle(other, data)
    assert report.created == 2 and report.skipped == []
    row = other.execute("SELECT * FROM bookmarks WHERE title = 'Lantern thread'").fetchone()
    assert row["platform"] == "X" and row["published_at"].startswith("20")
    assert row["published_source"] == "shared_import"
    assert row["saved_source"] == "shared_import" and row["saved_at"] is not None
    assert tags(other, "Lantern thread") == {"Rendering", "Announcement"}

    again = bundle.import_bundle(other, data)
    assert again.created == 0 and again.unchanged == 2
    other.close()


def test_a_shared_link_you_already_have_is_kept_aside_then_folded_in(filled, tmp_path):
    path, conn = filled
    saved_before = conn.execute(
        "SELECT saved_at FROM bookmarks WHERE title = 'Lantern talk'"
    ).fetchone()[0]
    data = {
        "format": "bookmark-atlas-bundle",
        "version": 1,
        "bookmarks": [
            {
                "url": "https://youtu.be/abc123XYZ_0",
                "title": "Their title",
                "tags": ["From a friend", "Talk"],
            }
        ],
    }
    report = bundle.import_bundle(conn, data)
    assert report.pending == 1 and report.created == 0
    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'Lantern talk'").fetchone()
    assert row["saved_at"] == saved_before  # their copy does not touch my dates or title
    assert "From a friend" not in tags(conn, "Lantern talk")

    groom.apply(conn, groom.get_script("merge-duplicates"))
    assert tags(conn, "Lantern talk") == {"Talk", "Rendering", "From a friend"}
    assert conn.execute("SELECT COUNT(*) FROM source_records WHERE pending").fetchone()[0] == 0


def test_bad_entries_are_skipped_and_reported(tmp_path):
    path, conn = make_db(tmp_path, "c", filled=False)
    data = {
        "format": "bookmark-atlas-bundle",
        "version": 1,
        "bookmarks": [
            {
                "url": "https://example.org/ok",
                "title": "Fine",
                "tags": ["A"],
                "published_at": "not a date",
            },
            {"url": "javascript:alert(1)", "title": "Script link"},
            {"title": "No link"},
            {"url": "https://example.org/x", "tags": "not a list"},
            {"url": "https://example.org/y", "title": 12},
            "not an object",
            {"url": "https://example.org/long", "title": "t" * 1001},
        ],
    }
    report = bundle.import_bundle(conn, data)
    assert report.created == 1
    assert [number for number, _ in report.skipped] == [2, 3, 4, 5, 6, 7]
    assert conn.execute("SELECT published_at FROM bookmarks").fetchone()[0] is None
    conn.close()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"hello": 1}, "not a Bookmark Atlas bundle"),
        ([1, 2], "not a Bookmark Atlas bundle"),
        ({"format": "bookmark-atlas-bundle", "version": 2, "bookmarks": []}, "Update the app"),
        ({"format": "bookmark-atlas-bundle", "version": 1}, "no list of bookmarks"),
    ],
)
def test_files_that_are_not_bundles_are_refused(tmp_path, data, message):
    path, conn = make_db(tmp_path, "d", filled=False)
    with pytest.raises(ValueError, match=message):
        bundle.import_bundle(conn, data)
    conn.close()


def test_cli_bundle_round_trip(filled, tmp_path, capsys):
    path, conn = filled
    out = tmp_path / "share.atlas.json"
    assert (
        main(
            [
                "--db",
                str(path),
                "export",
                "--format",
                "bundle",
                "--search",
                "lantern",
                "-o",
                str(out),
            ]
        )
        == 0
    )
    assert "bundle of 4 bookmarks" in capsys.readouterr().out

    other_path, other = make_db(tmp_path, "e", filled=False)
    other.close()
    assert main(["--db", str(other_path), "import-bundle", str(out), "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert main(["--db", str(other_path), "import-bundle", str(out)]) == 0
    assert "bookmarks created                  4" in capsys.readouterr().out

    assert main(["--db", str(other_path), "import-bundle", str(FIXTURE)]) == 1
    assert "not a Bookmark Atlas bundle" in capsys.readouterr().err
