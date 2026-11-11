import sqlite3

import pytest

from atlas import db
from atlas.cli import main


def test_init_creates_schema(tmp_path):
    path = tmp_path / "atlas.sqlite"
    assert main(["--db", str(path), "init"]) == 0

    conn = db.open_ready(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"bookmarks", "source_records", "tags", "bookmark_tags", "changes", "meta"} <= tables
    assert db.schema_version(conn) == db.latest_version()


def test_init_twice_is_a_no_op(tmp_path):
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    conn = db.connect(path)
    assert db.migrate(conn) == []


def test_missing_database_is_an_error_not_an_empty_file(tmp_path):
    path = tmp_path / "typo.sqlite"
    with pytest.raises(FileNotFoundError):
        db.open_ready(path)
    assert not path.exists()


def test_outdated_schema_is_refused(tmp_path):
    path = tmp_path / "old.sqlite"
    sqlite3.connect(path).close()  # an empty file: schema version 0
    with pytest.raises(RuntimeError, match="atlas init"):
        db.open_ready(path)


def test_foreign_keys_are_enforced(tmp_path):
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    conn = db.open_ready(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO bookmark_tags (bookmark_id, tag_id) VALUES ('nope', 1)")


def test_doctor_offline_reports_the_installation(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    path = tmp_path / "atlas.sqlite"

    assert main(["--db", str(path), "doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "not created yet" in out and "Notion token    not set" in out
    assert not path.exists()  # looking must not create anything

    main(["--db", str(path), "init"])
    assert main(["--db", str(path), "doctor", "--offline"]) == 0
    assert "schema 1, 0 bookmarks" in capsys.readouterr().out


def test_doctor_judges_the_notion_answer(tmp_path, capsys, monkeypatch):
    import atlas.cli as cli

    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_check_notion_reachable", lambda: (True, "reachable"))
    assert main(["--db", str(tmp_path / "a.sqlite"), "doctor"]) == 0
    monkeypatch.setattr(cli, "_check_notion_reachable", lambda: (False, "no route"))
    assert main(["--db", str(tmp_path / "a.sqlite"), "doctor"]) == 1
    assert "no route" in capsys.readouterr().out
