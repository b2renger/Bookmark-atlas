"""Opening the database and applying schema migrations.

The database is a single SQLite file. Migrations are numbered SQL files in
``atlas/migrations``; the number of the last one applied is stored in
``meta.schema_version``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

DEFAULT_DB_PATH = "data/atlas.sqlite"


def default_db_path() -> Path:
    """Path from ``ATLAS_DB`` if set, otherwise ``data/atlas.sqlite``."""
    return Path(os.environ.get("ATLAS_DB", DEFAULT_DB_PATH))


def now_iso() -> str:
    """Current time as ISO 8601 UTC with second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrations() -> list[tuple[int, str, str]]:
    """All migrations as ``(number, name, sql)``, in order."""
    found = []
    for entry in resources.files("atlas.migrations").iterdir():
        if entry.name.endswith(".sql"):
            number = int(entry.name.split("_", 1)[0])
            found.append((number, entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def latest_version() -> int:
    migrations = _migrations()
    return migrations[-1][0] if migrations else 0


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration newer than the database. Returns the names applied."""
    current = schema_version(conn)
    applied = []
    for number, name, sql in _migrations():
        if number <= current:
            continue
        # executescript commits on its own, so each migration is followed by
        # an explicit version bump; a crash in between is detected next time
        # because the tables already exist and the script fails loudly.
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(number),),
        )
        conn.commit()
        applied.append(name)
    return applied


def connect(path: Path | str, *, create: bool = False) -> sqlite3.Connection:
    """Open the database.

    With ``create=False`` a missing file is an error, so a typo in the path
    cannot silently start an empty database.
    """
    path = Path(path)
    if not path.exists():
        if not create:
            raise FileNotFoundError(f"No database at {path}. Run 'atlas init' first.")
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Deleting a run relies on ON DELETE CASCADE, which needs foreign_keys = ON:
# always open the database through connect().


def open_ready(path: Path | str) -> sqlite3.Connection:
    """Open an existing database and refuse to work on an outdated schema."""
    conn = connect(path)
    current, latest = schema_version(conn), latest_version()
    if current < latest:
        conn.close()
        raise RuntimeError(
            f"Database schema is version {current}, this version of Atlas needs {latest}. "
            "Run 'atlas init' to upgrade it."
        )
    if current > latest:
        conn.close()
        raise RuntimeError(
            f"Database schema is version {current}, newer than this version of Atlas ({latest})."
        )
    return conn
