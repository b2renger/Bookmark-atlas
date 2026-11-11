"""Bringing outside records into the database.

Import rules (see docs/IMPLEMENTATION_PLAN.md, section 5):

1. A record is matched to a bookmark by normalised URL.
2. No match: a new bookmark is created from the record.
3. Match with a deleted bookmark: the record is stored, nothing comes back.
4. Match with a live bookmark: the record is stored and marked pending for
   review. Curated fields are never overwritten.

Whatever happens, the record is kept exactly as received in
``source_records``. Importing the same content again changes nothing.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from atlas.dates import parse_date
from atlas.db import now_iso
from atlas.urls import domain_of, normalize_url

# Local field -> column headers it may be found under, compared case-insensitively.
DEFAULT_COLUMNS = {
    "title": ["Name", "Title"],
    "url": ["URL", "Link"],
    "summary": ["Description", "Summary"],
    "tags": ["Keywords", "Tags"],
    "date": ["Date", "Created", "Created time"],
}


@dataclass
class ImportReport:
    rows: int = 0
    created: int = 0
    unchanged: int = 0  # same content already imported
    pending: int = 0  # matched a live bookmark, stored for review
    tombstoned: int = 0  # matched a deleted bookmark, stays deleted
    date_only: int = 0  # dates without a time of day, left for split-dates
    unreadable_dates: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (row number, reason)
    dry_run: bool = False


def resolve_columns(headers: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Map local fields to the headers present in the file."""
    by_lower = {header.lower(): header for header in headers}
    mapping = {}
    for local, candidates in DEFAULT_COLUMNS.items():
        for candidate in candidates:
            if candidate.lower() in by_lower:
                mapping[local] = by_lower[candidate.lower()]
                break
    for local, header in (overrides or {}).items():
        if local not in DEFAULT_COLUMNS:
            raise ValueError(
                f"Unknown field {local!r}. Known fields: {', '.join(DEFAULT_COLUMNS)}."
            )
        if header.lower() not in by_lower:
            raise ValueError(f"No column named {header!r} in the file.")
        mapping[local] = by_lower[header.lower()]
    if "url" not in mapping:
        raise ValueError(
            "No URL column found. Columns in the file: "
            + ", ".join(headers)
            + ". Name it with --column url=<header>."
        )
    return mapping


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _split_tags(text: str | None) -> list[str]:
    seen, tags = set(), []
    for part in (text or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            tags.append(name)
    return tags


def _tag_id(conn: sqlite3.Connection, name: str) -> int:
    """ID of the tag to attach for ``name``, following merges."""
    row = conn.execute("SELECT id, alias_of FROM tags WHERE name = ?", (name,)).fetchone()
    if row is None:
        return conn.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
    seen = {row["id"]}
    while row["alias_of"] is not None and row["alias_of"] not in seen:
        row = conn.execute(
            "SELECT id, alias_of FROM tags WHERE id = ?", (row["alias_of"],)
        ).fetchone()
        seen.add(row["id"])
    return row["id"]


def find_bookmark(conn: sqlite3.Connection, url_normalized: str) -> sqlite3.Row | None:
    """Bookmark a normalised URL belongs to, following merges to the survivor.

    A live bookmark wins over a deleted one with the same URL.
    """
    row = conn.execute(
        "SELECT * FROM bookmarks WHERE url_normalized = ? "
        "ORDER BY deleted_at IS NOT NULL, created_at LIMIT 1",
        (url_normalized,),
    ).fetchone()
    seen = set()
    while row is not None and row["merged_into"] is not None and row["id"] not in seen:
        seen.add(row["id"])
        row = conn.execute("SELECT * FROM bookmarks WHERE id = ?", (row["merged_into"],)).fetchone()
    return row


def _store_record(conn, bookmark_id, source, raw, raw_hash, pending, external_id=None):
    conn.execute(
        "INSERT INTO source_records "
        "(bookmark_id, source, external_id, fetched_at, raw, raw_hash, pending) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bookmark_id, source, external_id, now_iso(), raw, raw_hash, int(pending)),
    )


def import_record(
    conn: sqlite3.Connection,
    report: ImportReport,
    *,
    source: str,
    raw: dict,
    url: str,
    title: str | None,
    summary: str | None,
    tags: list[str],
    date_text: str | None,
) -> None:
    """Apply the import rules to one record. Raises ValueError on a bad URL."""
    url_normalized = normalize_url(url)
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    raw_hash = _hash(raw_json)

    bookmark = find_bookmark(conn, url_normalized)
    if bookmark is not None:
        already = conn.execute(
            "SELECT 1 FROM source_records WHERE source = ? AND raw_hash = ? AND bookmark_id = ?",
            (source, raw_hash, bookmark["id"]),
        ).fetchone()
        if already:
            report.unchanged += 1
        elif bookmark["deleted_at"] is not None:
            _store_record(conn, bookmark["id"], source, raw_json, raw_hash, pending=False)
            report.tombstoned += 1
        else:
            _store_record(conn, bookmark["id"], source, raw_json, raw_hash, pending=True)
            report.pending += 1
        return

    saved_at = saved_source = None
    parsed = parse_date(date_text)
    if parsed is None:
        if date_text and date_text.strip():
            report.unreadable_dates += 1
    elif parsed.date_only:
        # Could be a save date or a publication date: the split-dates
        # grooming script decides, from the source record.
        report.date_only += 1
    else:
        saved_at, saved_source = parsed.iso, "date_column"

    bookmark_id = uuid.uuid4().hex
    now = now_iso()
    conn.execute(
        "INSERT INTO bookmarks (id, url, url_normalized, domain, title, summary, "
        "saved_at, saved_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bookmark_id,
            url.strip(),
            url_normalized,
            domain_of(url),
            (title or "").strip() or None,
            (summary or "").strip() or None,
            saved_at,
            saved_source,
            now,
            now,
        ),
    )
    for name in tags:
        conn.execute(
            "INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)",
            (bookmark_id, _tag_id(conn, name)),
        )
    _store_record(conn, bookmark_id, source, raw_json, raw_hash, pending=False)
    report.created += 1


def import_csv(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    columns: dict[str, str] | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import a CSV export. All rows succeed together or nothing is written."""
    report = ImportReport(dry_run=dry_run)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row.")
        mapping = resolve_columns(list(reader.fieldnames), columns)
        try:
            # Row numbers match what a spreadsheet shows: the header is row 1.
            for number, row in enumerate(reader, start=2):
                report.rows += 1
                url = (row.get(mapping["url"]) or "").strip()
                if not url:
                    report.skipped.append((number, "no URL"))
                    continue
                try:
                    import_record(
                        conn,
                        report,
                        source="csv",
                        raw={key: value for key, value in row.items() if key is not None},
                        url=url,
                        title=row.get(mapping.get("title", "")),
                        summary=row.get(mapping.get("summary", "")),
                        tags=_split_tags(row.get(mapping.get("tags", ""))),
                        date_text=row.get(mapping.get("date", "")),
                    )
                except ValueError as error:
                    report.skipped.append((number, str(error)))
        except BaseException:
            conn.rollback()
            raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report
