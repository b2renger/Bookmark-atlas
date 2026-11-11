import sqlite3
from pathlib import Path

import pytest

from atlas import db, groom
from atlas.cli import main
from atlas.groom import Change, GroomError
from atlas.groom.derive_platform import platform_for
from atlas.groom.normalize_tags import spelling_key
from atlas.groom.published_from_x_id import published_at
from atlas.importer import import_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "atlas.sqlite"
    main(["--db", str(target), "init"])
    return target


@pytest.fixture
def conn(path):
    connection = db.open_ready(path)
    import_csv(connection, FIXTURE)
    yield connection
    connection.close()


def state(conn):
    """Everything grooming may change, for exact before/after comparison."""
    return {
        "bookmarks": conn.execute(
            "SELECT id, platform, saved_at, saved_source, published_at, published_source, "
            "deleted_at IS NOT NULL, merged_into FROM bookmarks ORDER BY id"
        ).fetchall(),
        "tags": conn.execute(
            "SELECT bookmark_id, name FROM bookmark_tags JOIN tags ON tags.id = tag_id ORDER BY 1, 2"
        ).fetchall(),
        "aliases": conn.execute(
            "SELECT t.name, a.name FROM tags t JOIN tags a ON a.id = t.alias_of ORDER BY 1"
        ).fetchall(),
        "pending": conn.execute("SELECT id, pending FROM source_records ORDER BY id").fetchall(),
    }


def rows(result):
    return [tuple(row) for row in result]


def tags_of(conn, title):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id "
            "JOIN bookmarks ON bookmarks.id = bookmark_id WHERE title = ?",
            (title,),
        )
    }


def run(conn, name, params=None, **kwargs):
    return groom.apply(conn, groom.get_script(name), params, **kwargs)


# ---- engine ----------------------------------------------------------------


def test_builtin_scripts_are_listed():
    scripts, problems = groom.load_scripts()
    assert problems == []
    assert list(scripts) == [
        "merge-duplicates",
        "normalize-tags",
        "derive-platform",
        "published-from-x-id",
        "split-dates",
    ]
    assert all(script.title and script.description for script in scripts.values())


def test_preview_writes_nothing_and_is_stable(conn):
    before = {key: rows(value) for key, value in state(conn).items()}
    script = groom.get_script("derive-platform")

    first = groom.preview(conn, script)
    second = groom.preview(conn, script)

    assert first["changes"] == 5 and first["fingerprint"] == second["fingerprint"]
    assert {key: rows(value) for key, value in state(conn).items()} == before
    assert conn.execute("SELECT COUNT(*) FROM script_runs").fetchone()[0] == 0


def test_apply_journals_and_backs_up_then_undo_restores_exactly(conn, path):
    before = {key: rows(value) for key, value in state(conn).items()}

    results = [
        run(conn, name, db_path=path)
        for name in (
            "merge-duplicates",
            "normalize-tags",
            "derive-platform",
            "published-from-x-id",
            "split-dates",
        )
    ]

    assert all(result["applied"] for result in results)
    assert {key: rows(value) for key, value in state(conn).items()} != before
    journal = conn.execute(
        "SELECT COUNT(*) FROM changes WHERE script_run_id IS NOT NULL"
    ).fetchone()[0]
    assert journal == sum(result["applied"] for result in results)
    backups = list((path.parent / "backups").glob("atlas-*.sqlite"))
    assert len(backups) == 5
    assert sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 5

    for result in reversed(results):
        groom.undo(conn, result["run_id"], db_path=path)

    assert {key: rows(value) for key, value in state(conn).items()} == before
    assert {run["status"] for run in groom.history(conn)} == {"undone"}


def test_running_a_script_twice_does_nothing_the_second_time(conn):
    for name in (
        "merge-duplicates",
        "normalize-tags",
        "derive-platform",
        "published-from-x-id",
        "split-dates",
    ):
        assert run(conn, name)["applied"] > 0
        assert run(conn, name) == {"run_id": None, "applied": 0, "groups": 0, "backup": None}


def test_apply_refuses_when_the_data_moved_since_the_preview(conn):
    script = groom.get_script("derive-platform")
    seen = groom.preview(conn, script)
    conn.execute("UPDATE bookmarks SET platform = 'Mine' WHERE domain = 'x.com'")
    conn.commit()

    with pytest.raises(GroomError, match="Preview again"):
        groom.apply(conn, script, expected_fingerprint=seen["fingerprint"])
    assert conn.execute("SELECT COUNT(*) FROM script_runs").fetchone()[0] == 0


def test_excluded_groups_are_left_alone(conn):
    script = groom.get_script("derive-platform")
    seen = groom.preview(conn, script)
    assert {group["key"] for group in seen["groups"]} >= {"platform:X", "platform:GitHub"}

    result = groom.apply(
        conn, script, expected_fingerprint=seen["fingerprint"], exclude={"platform:X"}
    )

    assert result["applied"] == seen["changes"] - 1
    assert (
        conn.execute("SELECT platform FROM bookmarks WHERE domain = 'x.com'").fetchone()[0] is None
    )
    assert (
        conn.execute("SELECT platform FROM bookmarks WHERE domain = 'github.com'").fetchone()[0]
        == "GitHub"
    )


def test_undo_refuses_if_a_value_was_edited_afterwards_and_changes_nothing(conn):
    result = run(conn, "derive-platform")
    conn.execute("UPDATE bookmarks SET platform = 'Edited by hand' WHERE domain = 'x.com'")
    conn.commit()

    with pytest.raises(GroomError, match="changed in the meantime"):
        groom.undo(conn, result["run_id"])

    assert conn.execute("SELECT status FROM script_runs").fetchone()[0] == "applied"
    assert (
        conn.execute("SELECT platform FROM bookmarks WHERE domain = 'github.com'").fetchone()[0]
        == "GitHub"
    )


def test_undo_twice_is_refused(conn):
    result = run(conn, "derive-platform")
    groom.undo(conn, result["run_id"])
    with pytest.raises(GroomError, match="already been undone"):
        groom.undo(conn, result["run_id"])
    with pytest.raises(GroomError, match="No such run"):
        groom.undo(conn, "nope")


def test_unknown_script_and_parameters_are_explained(conn):
    with pytest.raises(GroomError, match="No grooming script"):
        groom.get_script("nope")
    script = groom.get_script("split-dates")
    with pytest.raises(GroomError, match="not a valid value"):
        groom.preview(conn, script, {"meaning": "whenever"})
    with pytest.raises(GroomError, match="Unknown parameter"):
        groom.preview(conn, script, {"speed": "fast"})


# ---- user scripts ----------------------------------------------------------

GOOD = """
from atlas.groom import Change

NAME = "shout-titles"
TITLE = "Upper-case titles"

def plan(conn, params):
    return [
        Change("bookmark", row["id"], "title", row["title"], row["title"].upper(), group=row["id"])
        for row in conn.execute("SELECT id, title FROM bookmarks WHERE title != upper(title)")
    ]
"""


def write_script(path, name, body):
    folder = groom.user_scripts_dir(path)
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(body, encoding="utf-8")


def test_a_user_script_runs_with_preview_journal_and_undo(conn, path):
    write_script(path, "shout.py", GOOD)
    scripts, problems = groom.load_scripts(path)
    assert problems == [] and not scripts["shout-titles"].builtin

    result = groom.apply(conn, scripts["shout-titles"], db_path=path)
    assert result["applied"] == 5
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE title = 'LANTERN TALK'").fetchone()[0]
        == 1
    )

    groom.undo(conn, result["run_id"])
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE title = 'Lantern talk'").fetchone()[0]
        == 1
    )


def test_a_broken_user_script_is_reported_and_does_not_hide_the_others(conn, path):
    write_script(path, "broken.py", "this is not python(")
    write_script(path, "noname.py", "def plan(conn, params):\n    return []\n")
    write_script(path, "clash.py", GOOD.replace("shout-titles", "derive-platform"))
    write_script(path, "_helper.py", "raise RuntimeError('never imported')")

    scripts, problems = groom.load_scripts(path)

    assert len(scripts) == 5
    assert sorted(problem["file"] for problem in problems) == ["broken.py", "clash.py", "noname.py"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("conn.execute(\"UPDATE bookmarks SET title = 'x'\")\n    return []", "tried to write"),
        ("return [Change('bookmark', 'a', 'url', 'x', 'y', group='g')]", "does not allow"),
        ("return [Change('bookmark', 'a', 'title', 'x', 'y', group='')]", "without a group"),
        ("return ['not a change']", "must return Change objects"),
        (
            "c = Change('bookmark', 'a', 'title', 'x', 'y', group='g')\n    return [c, c]",
            "same change twice",
        ),
    ],
)
def test_a_misbehaving_script_is_stopped_before_anything_is_written(conn, path, body, message):
    write_script(
        path,
        "bad.py",
        f"from atlas.groom import Change\nNAME = 'bad'\ndef plan(conn, params):\n    {body}\n",
    )
    script = groom.get_script("bad", path)
    before = {key: rows(value) for key, value in state(conn).items()}

    with pytest.raises(GroomError, match=message):
        groom.apply(conn, script)

    assert {key: rows(value) for key, value in state(conn).items()} == before
    assert conn.execute("SELECT title FROM bookmarks ORDER BY title LIMIT 1").fetchone()[0] != "x"


def test_a_change_based_on_a_wrong_old_value_aborts_the_whole_run(conn, path):
    write_script(
        path,
        "stale.py",
        "from atlas.groom import Change\nNAME = 'stale'\n"
        "def plan(conn, params):\n"
        "    ids = [r[0] for r in conn.execute('SELECT id FROM bookmarks ORDER BY id')]\n"
        "    return [Change('bookmark', ids[0], 'platform', None, 'A', group='1'),\n"
        "            Change('bookmark', ids[1], 'platform', 'not the real value', 'B', group='2')]\n",
    )
    with pytest.raises(GroomError, match="changed in the meantime"):
        groom.apply(conn, groom.get_script("stale", path))
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE platform IS NOT NULL").fetchone()[0] == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM script_runs").fetchone()[0] == 0


# ---- built-in scripts ------------------------------------------------------


def test_merge_duplicates_folds_the_held_row_in(conn):
    assert "Duplicate" not in tags_of(conn, "Lantern thread")

    result = run(conn, "merge-duplicates")

    assert result["groups"] == 1
    # "rendering" differs from "Rendering" only by case: tag spellings are another script's job.
    assert tags_of(conn, "Lantern thread") == {
        "Rendering",
        "Announcement",
        "rendering",
        "Duplicate",
    }
    assert conn.execute("SELECT COUNT(*) FROM source_records WHERE pending").fetchone()[0] == 0
    # The held row had a real save time and the bookmark had none.
    row = conn.execute(
        "SELECT saved_at, saved_source FROM bookmarks WHERE title = 'Lantern thread'"
    ).fetchone()
    assert tuple(row) == ("2026-06-20T07:30:00Z", "date_column")


def test_merge_duplicates_merges_bookmarks_sharing_a_link(conn):
    conn.execute(
        "UPDATE bookmarks SET url_normalized = (SELECT url_normalized FROM bookmarks WHERE title = 'lantern-core') "
        "WHERE title = 'Undated note'"
    )
    conn.execute(
        "UPDATE bookmarks SET created_at = '2020-01-01T00:00:00Z' WHERE title = 'lantern-core'"
    )
    conn.commit()

    result = run(conn, "merge-duplicates")
    keeper = conn.execute("SELECT * FROM bookmarks WHERE title = 'lantern-core'").fetchone()
    gone = conn.execute("SELECT * FROM bookmarks WHERE title = 'Undated note'").fetchone()

    assert keeper["deleted_at"] is None and gone["merged_into"] == keeper["id"]
    assert gone["deleted_at"].startswith("20") and "@" not in gone["deleted_at"]
    assert "Misc" in tags_of(conn, "lantern-core")

    groom.undo(conn, result["run_id"])
    gone = conn.execute("SELECT * FROM bookmarks WHERE title = 'Undated note'").fetchone()
    assert gone["deleted_at"] is None and gone["merged_into"] is None


def test_an_edited_row_is_not_mistaken_for_a_duplicate(conn):
    run(conn, "merge-duplicates")
    # A pending record that is the only one of its source on the bookmark: an upstream edit.
    bookmark = conn.execute("SELECT id FROM bookmarks WHERE title = 'Lantern talk'").fetchone()[0]
    conn.execute(
        "INSERT INTO source_records (bookmark_id, source, external_id, fetched_at, raw, content_hash, pending) "
        "VALUES (?, 'notion', 'page-1', '2026-01-01T00:00:00Z', '{}', 'h', 1)",
        (bookmark,),
    )
    conn.commit()
    assert groom.preview(conn, groom.get_script("merge-duplicates"))["changes"] == 0


def test_normalize_tags_keeps_the_most_used_spelling(conn):
    run(conn, "merge-duplicates")  # brings "rendering" next to "Rendering"
    result = run(conn, "normalize-tags")

    assert result["groups"] == 1
    assert "rendering" not in tags_of(conn, "Lantern thread")
    assert "Rendering" in tags_of(conn, "Lantern thread")
    assert rows(
        conn.execute("SELECT t.name, a.name FROM tags t JOIN tags a ON a.id = t.alias_of")
    ) == [("rendering", "Rendering")]


def test_a_merged_spelling_stays_merged_on_later_imports(conn, tmp_path):
    run(conn, "merge-duplicates")
    run(conn, "normalize-tags")
    extra = tmp_path / "extra.csv"
    extra.write_text(
        'Name,URL,Keywords\nNew,https://example.org/new,"rendering, Fresh"\n', encoding="utf-8"
    )
    import_csv(conn, extra)
    assert tags_of(conn, "New") == {"Rendering", "Fresh"}


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Computer Vision", "computer-vision", True),
        ("Three.js", "Threejs", True),
        ("3D Scanning", "3DScanning", True),
        ("AI/ML", "AIML", True),
        ("C", "C++", False),
        ("C", "C#", False),
        ("Gaussian Splatting", "3D Gaussian Splatting", False),
        ("LoRA", "Lora", True),
    ],
)
def test_spelling_key(a, b, same):
    assert (spelling_key(a) == spelling_key(b)) is same


def test_derive_platform(conn):
    run(conn, "derive-platform")
    found = dict(conn.execute("SELECT domain, platform FROM bookmarks"))
    assert found == {
        "example.org": "Website",
        "x.com": "X",
        "github.com": "GitHub",
        "youtube.com": "YouTube",
        "example.net": "Website",
    }
    assert platform_for("someone.github.io") == "GitHub Pages"
    assert platform_for("notgithub.io") == "Website"


def test_derive_platform_respects_existing_values_unless_told(conn):
    conn.execute("UPDATE bookmarks SET platform = 'Mine' WHERE domain = 'x.com'")
    conn.commit()
    run(conn, "derive-platform")
    assert (
        conn.execute("SELECT platform FROM bookmarks WHERE domain = 'x.com'").fetchone()[0]
        == "Mine"
    )
    run(conn, "derive-platform", {"overwrite": "true"})
    assert (
        conn.execute("SELECT platform FROM bookmarks WHERE domain = 'x.com'").fetchone()[0] == "X"
    )


def test_published_from_x_id(conn):
    # A real, well-known reference point: post 1 of the ID scheme's era is not
    # needed; the arithmetic is checked on a constructed ID.
    wanted_ms = 1_780_000_000_000  # 2026-05-28T20:26:40Z
    status_id = (wanted_ms - 1288834974657) << 22
    assert published_at(str(status_id)).strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-05-28T20:26:40Z"
    assert published_at("20") is None  # an ID from before the scheme
    assert published_at(str(1 << 62)) is None  # far in the future

    run(conn, "published-from-x-id")
    row = conn.execute(
        "SELECT published_at, published_source FROM bookmarks WHERE domain = 'x.com'"
    ).fetchone()
    assert row["published_source"] == "x_id" and row["published_at"].startswith("20")
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE published_at IS NOT NULL").fetchone()[0]
        == 1
    )


def test_split_dates_stores_date_only_values_and_skips_x_posts(conn):
    result = run(conn, "split-dates")

    assert result["groups"] == 1  # lantern-core; the X post is left to the other script
    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'lantern-core'").fetchone()
    assert (row["published_at"], row["published_source"], row["saved_at"]) == (
        "2026-01-09",
        "date_column",
        None,
    )
    assert (
        conn.execute("SELECT published_at FROM bookmarks WHERE domain = 'x.com'").fetchone()[0]
        is None
    )


def test_split_dates_can_treat_the_date_as_the_save_day(conn):
    run(conn, "split-dates", {"meaning": "saved"})
    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'lantern-core'").fetchone()
    assert (row["saved_at"], row["saved_source"], row["published_at"]) == (
        "2026-01-09",
        "date_column",
        None,
    )


def test_change_reads_well_in_a_preview():
    lines = groom.group_changes(
        [
            Change("bookmark", "b", "platform", None, "X", group="g", title="A bookmark"),
            Change("bookmark_tag", "b", "tag", None, "New", group="g"),
            Change("bookmark_tag", "b", "tag", "Old", None, group="g"),
        ]
    )
    assert lines == [
        {
            "key": "g",
            "title": "A bookmark",
            "lines": ["platform: empty → X", "add tag “New”", "remove tag “Old”"],
        }
    ]


# ---- command line ----------------------------------------------------------


def test_cli_preview_apply_history_undo(path, capsys):
    main(["--db", str(path), "import-csv", str(FIXTURE)])
    capsys.readouterr()

    assert main(["--db", str(path), "groom", "list"]) == 0
    assert "derive-platform" in capsys.readouterr().out

    assert main(["--db", str(path), "groom", "preview", "derive-platform"]) == 0
    out = capsys.readouterr().out
    assert "5 changes in 4 groups" in out and "Nothing was written" in out

    assert main(["--db", str(path), "groom", "apply", "derive-platform"]) == 0
    assert "applied 5 changes" in capsys.readouterr().out
    assert main(["--db", str(path), "groom", "apply", "derive-platform"]) == 0
    assert "nothing to do" in capsys.readouterr().out

    assert main(["--db", str(path), "groom", "history"]) == 0
    assert "applied" in capsys.readouterr().out
    assert main(["--db", str(path), "groom", "undo", "last"]) == 0
    assert "Reverted 5 changes" in capsys.readouterr().out
    assert main(["--db", str(path), "groom", "undo", "last"]) == 1
    assert "no applied run" in capsys.readouterr().err

    assert main(["--db", str(path), "groom", "preview", "nope"]) == 1
    assert "No grooming script" in capsys.readouterr().err
