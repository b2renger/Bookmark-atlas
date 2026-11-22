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
    assert f"schema {db.latest_version()}, 0 bookmarks" in capsys.readouterr().out


def test_doctor_judges_the_notion_answer(tmp_path, capsys, monkeypatch):
    import atlas.cli as cli

    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_check_notion_reachable", lambda: (True, "reachable"))
    assert main(["--db", str(tmp_path / "a.sqlite"), "doctor"]) == 0
    monkeypatch.setattr(cli, "_check_notion_reachable", lambda: (False, "no route"))
    assert main(["--db", str(tmp_path / "a.sqlite"), "doctor"]) == 1
    assert "no route" in capsys.readouterr().out


def test_a_migration_is_applied_whole_or_not_at_all(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "a.sqlite", create=True)
    db.migrate(conn)
    latest = db.latest_version()
    real = db._migrations()
    broken = (
        latest + 1,
        "9999_broken.sql",
        "CREATE TABLE half (id INTEGER);\nCREATE TABLE half (id INTEGER);",
    )
    monkeypatch.setattr(db, "_migrations", lambda: [*real, broken])
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    # Nothing of the failed migration stays: not its first table, not its number.
    assert db.schema_version(conn) == latest
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half'").fetchone()
    assert not conn.in_transaction
    mended = (
        latest + 1,
        "9999_mended.sql",
        "CREATE TABLE half (id INTEGER);\nCREATE TABLE other (id INTEGER);",
    )
    monkeypatch.setattr(db, "_migrations", lambda: [*real, mended])
    assert db.migrate(conn) == ["9999_mended.sql"] and db.schema_version(conn) == latest + 1
    conn.close()


def test_every_earlier_schema_upgrades_to_this_one(tmp_path, monkeypatch):
    real = db._migrations()
    for stop in range(1, len(real)):
        path = tmp_path / f"v{stop}.sqlite"
        monkeypatch.setattr(db, "_migrations", lambda stop=stop: real[:stop])
        conn = db.connect(path, create=True)
        db.migrate(conn)
        conn.close()
        monkeypatch.setattr(db, "_migrations", lambda: real)
        with pytest.raises(RuntimeError, match="Run 'atlas init'"):
            db.open_ready(path)
        assert [name for _, name, _ in real[stop:]] == db.upgrade(path)
        assert db.upgrade(path) == []
        conn = db.open_ready(path)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
