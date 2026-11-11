import csv
import json
import shutil
from pathlib import Path

import pytest

from atlas import db
from atlas.cli import main
from atlas.importer import import_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    connection = db.open_ready(path)
    yield connection
    connection.close()


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def snapshot(conn):
    """Everything an import could change, for before/after comparison."""
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in ("bookmarks", "source_records", "tags", "bookmark_tags")
    }


def test_first_import(conn):
    report = import_csv(conn, FIXTURE)

    assert report.rows == 8
    assert report.created == 5
    assert report.pending == 1  # the post saved twice under two domains
    assert [number for number, _ in report.skipped] == [8, 9]
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 5
    assert count(conn, "SELECT COUNT(*) FROM source_records") == 6


def test_bookmark_fields(conn):
    import_csv(conn, FIXTURE)
    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'Paper Lantern Renderer'").fetchone()

    assert row["url"] == "https://www.example.org/lantern/?utm_source=newsletter"  # kept as is
    assert row["url_normalized"] == "example.org/lantern"
    assert row["domain"] == "example.org"
    assert row["saved_at"] == "2026-03-03T15:12:00Z"
    assert row["saved_source"] == "date_column"
    assert row["published_at"] is None
    assert row["platform"] is None  # derived later by a grooming script

    tags = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id WHERE bookmark_id = ?",
            (row["id"],),
        )
    }
    assert tags == {"Rendering", "Path Tracing", "Open Source"}


def test_date_only_values_are_not_guessed(conn):
    report = import_csv(conn, FIXTURE)
    assert report.date_only == 2

    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'lantern-core'").fetchone()
    assert row["saved_at"] is None and row["published_at"] is None

    raw = conn.execute(
        "SELECT raw FROM source_records WHERE bookmark_id = ?", (row["id"],)
    ).fetchone()[0]
    assert json.loads(raw)["Date"] == "January 9, 2026 1:00 AM (GMT+1)"  # still available


def test_duplicate_is_stored_for_review_without_touching_the_bookmark(conn):
    import_csv(conn, FIXTURE)
    row = conn.execute(
        "SELECT * FROM bookmarks WHERE url_normalized = 'x.com/i/status/2030000000000000001'"
    ).fetchone()

    assert row["title"] == "Lantern thread"  # first one wins, second is not merged in
    records = conn.execute(
        "SELECT pending FROM source_records WHERE bookmark_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    assert [r[0] for r in records] == [0, 1]
    assert count(conn, "SELECT COUNT(*) FROM tags WHERE name = 'Duplicate'") == 0


def test_importing_twice_changes_nothing(conn):
    import_csv(conn, FIXTURE)
    before = snapshot(conn)

    report = import_csv(conn, FIXTURE)

    assert report.created == 0 and report.pending == 0
    assert report.unchanged == 6
    assert snapshot(conn) == before


def test_dry_run_writes_nothing(conn):
    report = import_csv(conn, FIXTURE, dry_run=True)
    assert report.created == 5
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 0
    assert count(conn, "SELECT COUNT(*) FROM source_records") == 0
    assert count(conn, "SELECT COUNT(*) FROM tags") == 0


def test_deleted_bookmark_does_not_come_back(conn, tmp_path):
    import_csv(conn, FIXTURE)
    conn.execute(
        "UPDATE bookmarks SET deleted_at = ? WHERE title = 'Lantern talk'", (db.now_iso(),)
    )
    conn.commit()

    # The same link arrives again with different content.
    changed = tmp_path / "changed.csv"
    with open(FIXTURE, encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    talk = next(r for r in rows if r["Name"] == "Lantern talk")
    talk["Description"] = "Edited upstream."
    with open(changed, "w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerow(talk)

    report = import_csv(conn, changed)

    assert report.tombstoned == 1 and report.created == 0 and report.pending == 0
    assert count(conn, "SELECT COUNT(*) FROM bookmarks WHERE title = 'Lantern talk'") == 1
    assert count(conn, "SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL") == 4


def test_record_follows_a_merge_to_the_surviving_bookmark(conn, tmp_path):
    import_csv(conn, FIXTURE)
    keep = conn.execute("SELECT id FROM bookmarks WHERE title = 'lantern-core'").fetchone()[0]
    gone = conn.execute("SELECT id FROM bookmarks WHERE title = 'Undated note'").fetchone()[0]
    conn.execute(
        "UPDATE bookmarks SET deleted_at = ?, merged_into = ? WHERE id = ?",
        (db.now_iso(), keep, gone),
    )
    conn.commit()

    extra = tmp_path / "extra.csv"
    extra.write_text(
        "Name,URL\nUndated note (edited),https://example.net/notes/undated\n", encoding="utf-8"
    )
    report = import_csv(conn, extra)

    assert report.pending == 1
    assert (
        count(conn, "SELECT COUNT(*) FROM source_records WHERE bookmark_id = ? AND pending", keep)
        == 1
    )


def test_missing_url_column_is_explained(conn, tmp_path):
    path = tmp_path / "odd.csv"
    path.write_text("Name,Address\nA,https://example.org\n", encoding="utf-8")

    with pytest.raises(ValueError, match="--column url="):
        import_csv(conn, path)

    report = import_csv(conn, path, columns={"url": "Address"})
    assert report.created == 1


def test_failure_midway_rolls_everything_back(conn, tmp_path, monkeypatch):
    import atlas.importer as importer

    real = importer.normalize_url
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk on fire")
        return real(url)

    monkeypatch.setattr(importer, "normalize_url", flaky)
    with pytest.raises(RuntimeError):
        import_csv(conn, FIXTURE)
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 0


def test_cli_import(tmp_path, capsys):
    path = tmp_path / "atlas.sqlite"
    copy = tmp_path / "export.csv"
    shutil.copy(FIXTURE, copy)
    main(["--db", str(path), "init"])

    assert main(["--db", str(path), "import-csv", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "bookmarks created" in out and "row 8: no URL" in out

    assert main(["--db", str(tmp_path / "missing.sqlite"), "import-csv", str(copy)]) == 1
    assert "atlas init" in capsys.readouterr().err
