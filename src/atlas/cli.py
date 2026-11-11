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


def _cmd_import_bundle(args: argparse.Namespace) -> int:
    import json

    from atlas.bundle import import_bundle

    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        raise ValueError(f"{args.file} is not a Bookmark Atlas bundle.") from None
    conn = db.open_ready(args.db)
    try:
        report = import_bundle(conn, data, dry_run=args.dry_run)
    finally:
        conn.close()
    print(("Dry run, nothing written: " if report.dry_run else "Imported ") + str(args.file))
    for label_, count in (
        ("bookmarks in the bundle", report.rows),
        ("bookmarks created", report.created),
        ("already imported", report.unchanged),
        ("links you already had", report.pending),
        ("deleted here, left deleted", report.tombstoned),
        ("entries skipped", len(report.skipped)),
    ):
        if count or label_ in ("bookmarks in the bundle", "bookmarks created"):
            print(f"  {label_:<30}{count:>6}")
    for number, reason in report.skipped[:20]:
        print(f"    entry {number}: {reason}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from atlas.exporter import export_bookmarks, matching_ids, write_csv, write_json

    conn = db.open_ready(args.db)
    try:
        if args.format == "bundle":
            import json

            from atlas.bundle import export_bundle

            ids = matching_ids(conn, args.search) if args.search else None
            data = export_bundle(conn, ids)
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                Path(args.output).write_text(text, encoding="utf-8")
                print(f"Wrote a bundle of {data['count']} bookmarks to {args.output}.")
            else:
                sys.stdout.write(text)
            return 0
        rows = export_bookmarks(conn, include_deleted=args.include_deleted)
        if args.search:
            keep = set(matching_ids(conn, args.search))
            rows = [row for row in rows if row["id"] in keep]
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
    requests = f"{report.requests} request" + ("" if report.requests == 1 else "s")
    print(f"{prefix}{report.table} ({kind}, {requests})")
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


def _cmd_ui(args: argparse.Namespace) -> int:
    from atlas.server import serve

    # The interface is the first thing a new user runs: set the database up.
    existed = Path(args.db).exists()
    conn = db.connect(args.db, create=True)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    if not existed and not args.announce:
        print(f"Created {args.db}.")
    serve(
        args.db,
        port=args.port,
        open_browser=not (args.no_browser or args.announce),
        announce=args.announce,
    )
    return 0


def _check_notion_reachable() -> tuple[bool, str]:
    """Contact Notion with a token that cannot be valid: a 401 proves the
    network path, TLS and certificates all work."""
    from atlas.notion import NotionClient

    try:
        NotionClient("ntn_connectivity_check_not_a_real_token").me()
    except NotionError as error:
        if error.status == 401:
            return True, "reachable (secure connection works)"
        return False, f"unexpected answer: {error.status} {error.code}"
    except ConnectionError as error:
        return False, str(error)
    return False, "unexpected answer: a made-up token was accepted"


def _cmd_doctor(args: argparse.Namespace) -> int:
    import platform
    import sqlite3

    from atlas import settings

    print(f"Bookmark Atlas {__version__}")
    print(f"  Python          {platform.python_version()} ({sys.executable})")
    print(f"  SQLite          {sqlite3.sqlite_version}")
    path = Path(args.db)
    if path.exists():
        conn = db.connect(path)
        try:
            version = db.schema_version(conn)
            count = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] if version else 0
        finally:
            conn.close()
        print(f"  Database        {path} (schema {version}, {count} bookmarks)")
    else:
        print(f"  Database        {path} (not created yet)")
    print(f"  Settings folder {settings.config_dir()}")
    source = settings.token_source()
    print(f"  Notion token    {'set, from ' + source if source else 'not set'}")
    if args.offline:
        return 0
    ok, detail = _check_notion_reachable()
    print(f"  Notion          {detail}")
    return 0 if ok else 1


def _groom_params(items) -> dict:
    params = {}
    for item in items or []:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise ValueError(f"--param expects name=value, got {item!r}.")
        params[name] = value
    return params


def _cmd_groom_list(args: argparse.Namespace) -> int:
    from atlas import groom

    scripts, problems = groom.load_scripts(args.db)
    for script in scripts.values():
        origin = "" if script.builtin else "  (yours)"
        print(f"{script.name:<22}{script.title}{origin}")
        for spec in script.params:
            print(f"{'':<22}  --param {spec['name']}=…  {spec.get('label', '')}")
    for problem in problems:
        print(f"could not load {problem['file']}: {problem['error']}", file=sys.stderr)
    print(f"\nYour own scripts go in {groom.user_scripts_dir(args.db)}")
    return 0


def _print_groups(result: dict, limit: int) -> None:
    for group in result["groups"][:limit]:
        print(f"  {group['title']}")
        for line in group["lines"][:6]:
            print(f"      {line}")
        if len(group["lines"]) > 6:
            print(f"      … and {len(group['lines']) - 6} more")
    hidden = result["groups_total"] - min(limit, len(result["groups"]))
    if hidden > 0:
        print(f"  … and {hidden} more groups")


def _cmd_groom_preview(args: argparse.Namespace) -> int:
    from atlas import groom

    conn = db.open_ready(args.db)
    try:
        script = groom.get_script(args.script, args.db)
        result = groom.preview(conn, script, _groom_params(args.param), limit=args.show)
    finally:
        conn.close()
    print(f"{script.title}: {result['changes']} changes in {result['groups_total']} groups")
    _print_groups(result, args.show)
    if result["changes"]:
        print("Nothing was written. Apply with: atlas groom apply " + args.script)
    return 0


def _cmd_groom_apply(args: argparse.Namespace) -> int:
    from atlas import groom

    conn = db.open_ready(args.db)
    try:
        script = groom.get_script(args.script, args.db)
        result = groom.apply(conn, script, _groom_params(args.param), db_path=args.db)
    finally:
        conn.close()
    if not result["applied"]:
        print(f"{script.title}: nothing to do.")
        return 0
    print(f"{script.title}: applied {result['applied']} changes in {result['groups']} groups.")
    print(f"  backup: {result['backup']}")
    print(f"  undo with: atlas groom undo {result['run_id']}")
    return 0


def _cmd_groom_undo(args: argparse.Namespace) -> int:
    from atlas import groom

    conn = db.open_ready(args.db)
    try:
        run_id = args.run
        if run_id == "last":
            applied = [run for run in groom.history(conn) if run["status"] == "applied"]
            if not applied:
                raise ValueError("There is no applied run to undo.")
            run_id = applied[0]["run_id"]
        result = groom.undo(conn, run_id, db_path=args.db)
    finally:
        conn.close()
    print(f"Reverted {result['reverted']} changes.")
    return 0


def _cmd_groom_history(args: argparse.Namespace) -> int:
    from atlas import groom

    conn = db.open_ready(args.db)
    try:
        runs = groom.history(conn)
    finally:
        conn.close()
    if not runs:
        print("No grooming run yet.")
    for run in runs:
        print(
            f"{run['started_at']}  {run['script']:<22}{run['changes']:>6} changes  "
            f"{run['status']:<8} {run['run_id']}"
        )
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

    import_bundle = commands.add_parser(
        "import-bundle", help="import a bundle shared by another Atlas user"
    )
    import_bundle.add_argument("file", type=Path)
    import_bundle.add_argument("--dry-run", action="store_true", help="report without writing")
    import_bundle.set_defaults(func=_cmd_import_bundle)

    export = commands.add_parser("export", help="write the bookmarks as CSV, JSON or a bundle")
    export.add_argument(
        "--format",
        choices=["csv", "json", "bundle"],
        default="csv",
        help="bundle is the format to share with another Atlas user",
    )
    export.add_argument("--search", help="only bookmarks matching this text")
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

    ui = commands.add_parser("ui", help="open the interface in the browser")
    ui.add_argument(
        "--port", type=int, default=8765, help="local port (default: 8765; 0 picks a free one)"
    )
    ui.add_argument(
        "--announce",
        action="store_true",
        help="print the address as one machine-readable line (used by the desktop app)",
    )
    ui.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ui.set_defaults(func=_cmd_ui)

    doctor = commands.add_parser("doctor", help="check that this installation works")
    doctor.add_argument("--offline", action="store_true", help="skip the Notion connection check")
    doctor.set_defaults(func=_cmd_doctor)

    groom = commands.add_parser("groom", help="clean the collection with scripts")
    groom_commands = groom.add_subparsers(dest="groom_command", required=True)

    groom_list = groom_commands.add_parser("list", help="list the grooming scripts")
    groom_list.set_defaults(func=_cmd_groom_list)

    for name, func, text in (
        ("preview", _cmd_groom_preview, "show what a script would change, without writing"),
        ("apply", _cmd_groom_apply, "run a script and write its changes"),
    ):
        sub = groom_commands.add_parser(name, help=text)
        sub.add_argument("script")
        sub.add_argument("--param", action="append", metavar="NAME=VALUE")
        if name == "preview":
            sub.add_argument("--show", type=int, default=15, help="groups to print (default 15)")
        sub.set_defaults(func=func)

    groom_undo = groom_commands.add_parser("undo", help="reverse a run")
    groom_undo.add_argument("run", help="run ID from 'atlas groom history', or 'last'")
    groom_undo.set_defaults(func=_cmd_groom_undo)

    groom_history = groom_commands.add_parser("history", help="list past runs")
    groom_history.set_defaults(func=_cmd_groom_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError, ConnectionError, NotionError) as error:
        print(f"atlas: {error}", file=sys.stderr)
        return 1
