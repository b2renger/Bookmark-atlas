"""Command line entry point: ``atlas <command>``.

Every feature of the app is reachable from here, so the desktop interface is
only another caller of the same functions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas import __version__, db
from atlas.notion import NotionError


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


def _cmd_import_csv(args: argparse.Namespace) -> int:
    from atlas.importer import import_csv

    columns = {}
    for item in args.column or []:
        local, separator, header = item.partition("=")
        if not separator or not local or not header:
            raise ValueError(f"--column expects field=header, got {item!r}.")
        columns[local] = header

    conn = db.open_ready(args.db)
    try:
        report = import_csv(conn, args.file, columns=columns, dry_run=args.dry_run)
    finally:
        conn.close()

    print(("Dry run, nothing written: " if report.dry_run else "Imported ") + str(args.file))
    lines = [
        ("rows read", report.rows, ""),
        ("bookmarks created", report.created, ""),
        ("already imported", report.unchanged, ""),
        ("matched an existing bookmark", report.pending, "stored for review"),
        ("matched a deleted bookmark", report.tombstoned, "stays deleted"),
        ("date without time of day", report.date_only, "left for the split-dates script"),
        ("unreadable dates", report.unreadable_dates, ""),
        ("rows skipped", len(report.skipped), ""),
    ]
    for label, count, note in lines:
        if count or label in ("rows read", "bookmarks created"):
            print(f"  {label:<30}{count:>6}" + (f"  ({note})" if note else ""))
    for number, reason in report.skipped[:20]:
        print(f"    row {number}: {reason}")
    if len(report.skipped) > 20:
        print(f"    ... and {len(report.skipped) - 20} more")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from atlas.exporter import export_bookmarks, write_csv, write_json

    conn = db.open_ready(args.db)
    try:
        rows = export_bookmarks(conn, include_deleted=args.include_deleted)
    finally:
        conn.close()

    write = write_csv if args.format == "csv" else write_json
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as handle:
            write(rows, handle)
        print(f"Wrote {len(rows)} bookmarks to {args.output}.")
    else:
        write(rows, sys.stdout)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from atlas.exporter import status

    conn = db.open_ready(args.db)
    try:
        info = status(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()

    print(f"{args.db} (schema version {version})")
    lines = [
        ("bookmarks", info["bookmarks"]),
        ("deleted or merged", info["deleted"]),
        ("source records", info["source_records"]),
        ("waiting for review", info["pending_review"]),
        ("tags", info["tags"]),
        ("tags used once", info["tags_used_once"]),
        ("bookmarks without tags", info["without_tags"]),
        ("without save date", info["without_saved_at"]),
        ("without publication date", info["without_published_at"]),
        ("without platform", info["without_platform"]),
    ]
    for label, count in lines:
        print(f"  {label:<26}{count:>6}")
    if info["top_domains"]:
        print("  top domains")
        for domain, count in info["top_domains"]:
            print(f"    {domain:<24}{count:>6}")
    return 0


def _notion_client():
    from atlas import settings
    from atlas.notion import NotionClient

    token = settings.get_token()
    if not token:
        raise ValueError(
            "No Notion token. Run 'atlas notion token', or enter it in Settings ('atlas ui')."
        )
    return NotionClient(token)


def _cmd_notion_token(args: argparse.Namespace) -> int:
    import getpass

    from atlas import settings
    from atlas.notion import NotionClient

    if args.forget:
        settings.delete_token()
        print("Stored Notion token removed.")
        return 0
    token = getpass.getpass("Notion token (input hidden): ").strip()
    who = NotionClient(token).me()  # refuse to store a token that does not work
    where = settings.set_token(token)
    name = who["name"] or "unnamed connection"
    workspace = f" in {who['workspace']}" if who["workspace"] else ""
    place = "the system keychain" if where == "keychain" else str(settings.config_dir())
    print(f"Token accepted: {name}{workspace}. Stored in {place}.")
    return 0


def _cmd_notion_tables(args: argparse.Namespace) -> int:
    tables = _notion_client().list_data_sources()
    if not tables:
        print("No table is shared with this connection yet. Share a database with it in Notion.")
        return 0
    for table in tables:
        print(f"{table['id']}  {table['title']}  ({len(table['columns'])} columns)")
    return 0


def _cmd_notion_select(args: argparse.Namespace) -> int:
    from atlas.notion_sync import select_data_source

    mapping = {}
    for item in args.column or []:
        local, separator, column = item.partition("=")
        if not separator or not local or not column:
            raise ValueError(f"--column expects field=column, got {item!r}.")
        mapping[local] = column

    client = _notion_client()
    conn = db.open_ready(args.db)
    try:
        if mapping:
            from atlas.notion_sync import guess_property_map

            columns = client.retrieve_data_source(args.table)["columns"]
            mapping = {**guess_property_map(columns), **mapping}
        choice = select_data_source(conn, client, args.table, mapping or None)
    finally:
        conn.close()
    print(f"Selected table: {choice['title']}")
    for local, column in choice["property_map"].items():
        print(f"  {local:<8} <- {column}")
    return 0


def _print_sync_report(report) -> None:
    kind = "full" if report.full else "incremental"
    prefix = "Dry run, nothing written: " if report.dry_run else "Synced "
    print(f"{prefix}{report.table} ({kind}, {report.requests} requests)")
    lines = [
        ("rows received", report.rows, ""),
        ("bookmarks created", report.created, ""),
        ("already up to date", report.unchanged, ""),
        ("linked to existing bookmarks", report.linked, "same content, nothing to review"),
        ("changed in Notion", report.pending, "stored for review"),
        ("deleted here, left deleted", report.tombstoned, ""),
        ("empty save dates filled", report.filled_saved_at, "from Notion creation time"),
        ("in Notion's trash", report.in_trash, "ignored"),
        ("date without time of day", report.date_only, "left for the split-dates script"),
        ("rows skipped", len(report.skipped), ""),
    ]
    for label, count, note in lines:
        if count or label in ("rows received", "bookmarks created"):
            print(f"  {label:<30}{count:>6}" + (f"  ({note})" if note else ""))
    for key, reason in report.skipped[:20]:
        print(f"    {key}: {reason}")
    if report.created_time_compared:
        print(
            f"  Notion creation time matches the Date column on "
            f"{report.created_time_agrees} of {report.created_time_compared} rows that have both."
        )


def _cmd_sync(args: argparse.Namespace) -> int:
    from atlas.notion_sync import sync

    client = _notion_client()
    conn = db.open_ready(args.db)
    try:
        report = sync(conn, client, full=args.full, dry_run=args.dry_run)
    finally:
        conn.close()
    _print_sync_report(report)
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

    import_csv = commands.add_parser("import-csv", help="import a CSV export of the bookmarks")
    import_csv.add_argument("file", type=Path)
    import_csv.add_argument(
        "--column",
        action="append",
        metavar="FIELD=HEADER",
        help="name the column holding a field (title, url, summary, tags, date); repeatable",
    )
    import_csv.add_argument(
        "--dry-run", action="store_true", help="report what would happen without writing"
    )
    import_csv.set_defaults(func=_cmd_import_csv)

    export = commands.add_parser("export", help="write the bookmarks as CSV or JSON")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument(
        "-o", "--output", type=Path, help="file to write (default: standard output)"
    )
    export.add_argument(
        "--include-deleted", action="store_true", help="also export deleted and merged bookmarks"
    )
    export.set_defaults(func=_cmd_export)

    status = commands.add_parser("status", help="summarise the state of the collection")
    status.set_defaults(func=_cmd_status)

    notion = commands.add_parser("notion", help="connect to Notion and choose a table")
    notion_commands = notion.add_subparsers(dest="notion_command", required=True)

    token = notion_commands.add_parser("token", help="enter and store the Notion token")
    token.add_argument("--forget", action="store_true", help="remove the stored token")
    token.set_defaults(func=_cmd_notion_token)

    tables = notion_commands.add_parser("tables", help="list the tables shared with the connection")
    tables.set_defaults(func=_cmd_notion_tables)

    select = notion_commands.add_parser("select", help="choose the table to import from")
    select.add_argument("table", help="ID shown by 'atlas notion tables'")
    select.add_argument(
        "--column",
        action="append",
        metavar="FIELD=COLUMN",
        help="override which column feeds a field (title, url, summary, tags, date)",
    )
    select.set_defaults(func=_cmd_notion_select)

    sync = commands.add_parser("sync", help="import new and edited rows from the Notion table")
    sync.add_argument("--full", action="store_true", help="read the whole table again")
    sync.add_argument("--dry-run", action="store_true", help="report without writing")
    sync.set_defaults(func=_cmd_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError, ConnectionError, NotionError) as error:
        print(f"atlas: {error}", file=sys.stderr)
        return 1
