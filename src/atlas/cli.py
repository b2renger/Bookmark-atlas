"""Command line entry point: ``atlas <command>``.

Every feature of the app is reachable from here, so the desktop interface is
only another caller of the same functions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas import __version__, db


def _cmd_init(args: argparse.Namespace) -> int:
    existed = Path(args.db).exists()
    conn = db.connect(args.db, create=True)
    try:
        applied = db.migrate(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()
    if not existed:
        print(f"Created {args.db} (schema version {version}).")
    elif applied:
        print(f"Upgraded {args.db} to schema version {version}: {', '.join(applied)}.")
    else:
        print(f"{args.db} is already at schema version {version}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas", description="Bookmark Atlas")
    parser.add_argument("--version", action="version", version=f"atlas {__version__}")
    parser.add_argument(
        "--db",
        default=str(db.default_db_path()),
        help="path to the database file (default: $ATLAS_DB or data/atlas.sqlite)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create the database or upgrade its schema")
    init.set_defaults(func=_cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"atlas: {error}", file=sys.stderr)
        return 1
