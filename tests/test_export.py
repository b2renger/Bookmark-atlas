import csv
import io
import json
from pathlib import Path

import pytest

from atlas import db
from atlas.cli import main
from atlas.exporter import EXPORT_FIELDS, export_bookmarks, status, write_csv, write_json
from atlas.importer import import_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    connection = db.open_ready(path)
    import_csv(connection, FIXTURE)
    yield connection
    connection.close()


def test_export_rows(conn):
    rows = export_bookmarks(conn)

    assert len(rows) == 5
    assert list(rows[0].keys()) == EXPORT_FIELDS
    lantern = next(r for r in rows if r["title"] == "Paper Lantern Renderer")
    assert lantern["tags"] == ["Open Source", "Path Tracing", "Rendering"]
    assert lantern["saved_at"] == "2026-03-03T15:12:00Z"


def test_deleted_bookmarks_are_left_out_unless_asked(conn):
    conn.execute(
        "UPDATE bookmarks SET deleted_at = ? WHERE title = 'Lantern talk'", (db.now_iso(),)
    )
    conn.commit()

    assert len(export_bookmarks(conn)) == 4
    everything = export_bookmarks(conn, include_deleted=True)
    assert len(everything) == 5
    assert sum(1 for r in everything if r["deleted_at"]) == 1


def test_csv_round_trip(conn):
    rows = export_bookmarks(conn)
    buffer = io.StringIO()
    write_csv(rows, buffer)

    read = list(csv.DictReader(io.StringIO(buffer.getvalue())))
    assert [r["url"] for r in read] == [r["url"] for r in rows]
    assert read[0].keys() == set(EXPORT_FIELDS)
    lantern = next(r for r in read if r["title"] == "Paper Lantern Renderer")
    assert lantern["tags"] == "Open Source, Path Tracing, Rendering"
    undated = next(r for r in read if r["title"] == "Undated note")
    assert undated["saved_at"] == ""


def test_json_keeps_tags_as_a_list_and_text_readable(conn):
    conn.execute(
        "UPDATE bookmarks SET title = 'Lanterne en papier – été' WHERE domain = 'example.org'"
    )
    buffer = io.StringIO()
    write_json(export_bookmarks(conn), buffer)

    assert "Lanterne en papier – été" in buffer.getvalue()  # not escaped
    data = json.loads(buffer.getvalue())
    assert isinstance(data[0]["tags"], list)


def test_empty_database_exports_a_header_only(tmp_path):
    path = tmp_path / "empty.sqlite"
    main(["--db", str(path), "init"])
    empty = db.open_ready(path)
    buffer = io.StringIO()
    write_csv(export_bookmarks(empty), buffer)
    assert buffer.getvalue().strip() == ",".join(EXPORT_FIELDS)


def test_status_counts(conn):
    info = status(conn)

    assert info["bookmarks"] == 5
    assert info["deleted"] == 0
    assert info["source_records"] == 6
    assert info["pending_review"] == 1
    assert info["without_saved_at"] == 3  # two date-only rows and one undated
    assert info["without_published_at"] == 5
    assert info["without_tags"] == 0
    assert ("x.com", 1) in info["top_domains"]


def test_cli_export_and_status(tmp_path, capsys):
    path = tmp_path / "atlas.sqlite"
    out_file = tmp_path / "out.json"
    main(["--db", str(path), "init"])
    main(["--db", str(path), "import-csv", str(FIXTURE)])
    capsys.readouterr()

    assert main(["--db", str(path), "export", "--format", "json", "-o", str(out_file)]) == 0
    assert len(json.loads(out_file.read_text(encoding="utf-8"))) == 5
    capsys.readouterr()

    assert main(["--db", str(path), "export"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == ",".join(EXPORT_FIELDS)

    assert main(["--db", str(path), "status"]) == 0
    assert "waiting for review" in capsys.readouterr().out
