"""Choosing a Notion table and importing its rows.

The sync is incremental: it remembers the latest edit time it has seen and
next time asks only for rows edited since. Rows go through the same import
rules as any other record (see ``atlas.importer``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from atlas.dates import parse_date
from atlas.importer import ImportReport, import_record, split_tags
from atlas.notion import NotionClient, plain_text

FIELDS = ("title", "url", "summary", "tags", "date")

# For each local field: acceptable column types, then preferred column names.
_GUESS = {
    "title": (("title",), ()),
    "url": (("url",), ("url", "link")),
    "summary": (("rich_text",), ("description", "summary", "notes", "résumé", "resume")),
    "tags": (("multi_select",), ("keywords", "tags", "mots-clés")),
    "date": (("date", "created_time"), ("date", "saved", "created")),
}

SELECTED_KEY = "notion_selected"


@dataclass
class SyncReport(ImportReport):
    table: str = ""
    full: bool = False
    in_trash: int = 0
    requests: int = 0
    # Does Notion's page creation time match the Date column, where both exist?
    created_time_compared: int = 0
    created_time_agrees: int = 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["skipped"] = [{"id": str(key), "reason": reason} for key, reason in self.skipped]
        return data


def guess_property_map(columns: list[dict]) -> dict[str, str]:
    """Best guess of which column feeds which field. The user can correct it."""
    mapping = {}
    for local, (types, names) in _GUESS.items():
        candidates = [column for column in columns if column["type"] in types]
        if not candidates:
            continue
        chosen = next(
            (
                column
                for name in names
                for column in candidates
                if column["name"].strip().lower() == name
            ),
            candidates[0],
        )
        mapping[local] = chosen["name"]
    return mapping


def validate_property_map(columns: list[dict], mapping: dict[str, str]) -> dict[str, str]:
    names = {column["name"] for column in columns}
    cleaned = {}
    for local, column in mapping.items():
        if local not in FIELDS:
            raise ValueError(f"Unknown field {local!r}. Known fields: {', '.join(FIELDS)}.")
        if not column:
            continue
        if column not in names:
            raise ValueError(f"The table has no column named {column!r}.")
        cleaned[local] = column
    if "url" not in cleaned:
        raise ValueError("Choose which column holds the URL.")
    return cleaned


def _date_text(value: dict | None) -> str | None:
    """A Notion date value as text ``parse_date`` understands."""
    if not value or not value.get("start"):
        return None
    start = value["start"]
    zone = value.get("time_zone")
    if zone and "T" in start:
        try:
            moment = datetime.fromisoformat(start)
            if moment.tzinfo is None:
                return moment.replace(tzinfo=ZoneInfo(zone)).isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return start


def _value(prop: dict | None, *, as_list: bool = False):
    """Read one page property, whatever its type, as text or a list of names."""
    if not prop:
        return [] if as_list else None
    kind = prop.get("type")
    data = prop.get(kind)
    if kind in ("title", "rich_text"):
        text = plain_text(data)
        return split_tags(text) if as_list else text
    if kind == "multi_select":
        names = [option.get("name", "") for option in data or [] if option.get("name")]
        return names if as_list else ", ".join(names)
    if kind in ("select", "status"):
        name = (data or {}).get("name")
        return ([name] if name else []) if as_list else name
    if kind == "date":
        text = _date_text(data)
        return ([text] if text else []) if as_list else text
    if kind in ("url", "email", "phone_number", "created_time", "last_edited_time"):
        return ([data] if data else []) if as_list else data
    return [] if as_list else None


def extract(page: dict, mapping: dict[str, str]) -> dict:
    """The mapped fields of one Notion page."""
    properties = page.get("properties") or {}

    def prop(local: str) -> dict | None:
        return properties.get(mapping[local]) if local in mapping else None

    return {
        "title": _value(prop("title")),
        "url": _value(prop("url")),
        "summary": _value(prop("summary")),
        "tags": _value(prop("tags"), as_list=True),
        "date_text": _value(prop("date")),
    }


def selected(conn: sqlite3.Connection) -> dict | None:
    """The table chosen for import, with its column mapping and sync mark."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SELECTED_KEY,)).fetchone()
    if row is None:
        return None
    info = json.loads(row[0])
    state = conn.execute(
        "SELECT * FROM sync_state WHERE data_source_id = ?", (info["data_source_id"],)
    ).fetchone()
    if state is None:
        return None
    return {
        "data_source_id": info["data_source_id"],
        "title": info.get("title", ""),
        "property_map": json.loads(state["property_map"]),
        "last_synced_time": state["last_synced_time"],
    }


def select_data_source(
    conn: sqlite3.Connection,
    client: NotionClient,
    data_source_id: str,
    property_map: dict[str, str] | None = None,
) -> dict:
    """Choose the table to import from. Keeps the sync mark if it was already chosen."""
    table = client.retrieve_data_source(data_source_id)
    mapping = validate_property_map(
        table["columns"], property_map or guess_property_map(table["columns"])
    )
    conn.execute(
        "INSERT INTO sync_state (data_source_id, database_id, property_map) VALUES (?, ?, ?) "
        "ON CONFLICT(data_source_id) DO UPDATE SET "
        "database_id = excluded.database_id, property_map = excluded.property_map",
        (table["id"], table["database_id"] or "", json.dumps(mapping, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (
            SELECTED_KEY,
            json.dumps(
                {"data_source_id": table["id"], "title": table["title"]}, ensure_ascii=False
            ),
        ),
    )
    conn.commit()
    return selected(conn)


def _moment(text: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(
    conn: sqlite3.Connection,
    client: NotionClient,
    *,
    full: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Import rows from the selected table. One transaction: all or nothing."""
    choice = selected(conn)
    if choice is None:
        raise ValueError("No Notion table selected. Choose one in Settings first.")
    mapping = choice["property_map"]
    mark = _moment(choice["last_synced_time"])
    since = None if full or mark is None else _iso(mark)

    report = SyncReport(dry_run=dry_run, table=choice["title"], full=since is None)
    latest = mark
    before = client.requests_made
    try:
        for page in client.query_pages(choice["data_source_id"], edited_since=since):
            report.rows += 1
            edited = _moment(page.get("last_edited_time"))
            if edited and (latest is None or edited > latest):
                latest = edited
            if page.get("in_trash"):
                report.in_trash += 1
                continue

            fields = extract(page, mapping)
            if not (fields["url"] or "").strip():
                report.skipped.append((page.get("id", "?"), "no URL"))
                continue

            created = _moment(page.get("created_time"))
            parsed = parse_date(fields["date_text"])
            if created and parsed and not parsed.date_only:
                report.created_time_compared += 1
                if abs((created - _moment(parsed.iso)).total_seconds()) <= 120:
                    report.created_time_agrees += 1

            try:
                import_record(
                    conn,
                    report,
                    source="notion",
                    raw=page,
                    external_id=page["id"],
                    external_edited_at=page.get("last_edited_time"),
                    saved_at=_iso(created) if created else None,
                    saved_source="notion_created" if created else None,
                    **fields,
                )
            except ValueError as error:
                report.skipped.append((page.get("id", "?"), str(error)))
    except BaseException:
        conn.rollback()
        raise
    report.requests = client.requests_made - before

    if dry_run:
        conn.rollback()
        return report
    if latest is not None:
        # Moved only now, after every row went through: an interrupted sync
        # leaves the old mark and is simply repeated.
        conn.execute(
            "UPDATE sync_state SET last_synced_time = ? WHERE data_source_id = ?",
            (_iso(latest), choice["data_source_id"]),
        )
    conn.commit()
    return report
