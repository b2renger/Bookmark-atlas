"""Bringing outside records into the database.

Import rules (see docs/IMPLEMENTATION_PLAN.md, section 5):

1. A record is matched to a bookmark by its external ID (a Notion page ID)
   when it has one, otherwise by normalised URL.
2. No match: a new bookmark is created from the record.
3. Match with a deleted bookmark: the record is stored, nothing comes back.
4. Match with a live bookmark: the record is stored. If it says something no
   earlier record of that bookmark said, it is marked pending for review.
   Curated fields are never overwritten; an empty save date may be filled.

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
    linked: int = 0  # known content arriving from a new source, attached quietly
    pending: int = 0  # new content for a live bookmark, stored for review
    tombstoned: int = 0  # matched a deleted bookmark, stays deleted
    date_only: int = 0  # dates without a time of day, left for split-dates
    unreadable_dates: int = 0
    filled_saved_at: int = 0  # empty save dates filled from the source
    skipped: list[tuple[int | str, str]] = field(default_factory=list)  # (row or ID, reason)
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


def split_tags(text: str | None) -> list[str]:
    seen, tags = set(), []
    for part in (text or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            tags.append(name)
    return tags


def content_hash(
    url: str, title: str | None, summary: str | None, tags: list[str], date_text: str | None
) -> str:
    """Hash of what a record says, independent of where it came from.

    A CSV row and the Notion page it was exported from should hash the same:
    tag order is ignored and dates are compared as parsed values.
    """
    parsed = parse_date(date_text)
    content = {
        "url": url.strip(),
        "title": (title or "").strip(),
        "summary": (summary or "").strip(),
        "tags": sorted(set(tags)),
        "date": parsed.iso if parsed else (date_text or "").strip(),
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def _survivor(conn: sqlite3.Connection, row: sqlite3.Row | None) -> sqlite3.Row | None:
    """Follow ``merged_into`` to the bookmark that absorbed this one."""
    seen = set()
    while row is not None and row["merged_into"] is not None and row["id"] not in seen:
        seen.add(row["id"])
        row = conn.execute("SELECT * FROM bookmarks WHERE id = ?", (row["merged_into"],)).fetchone()
    return row


def find_bookmark(conn: sqlite3.Connection, url_normalized: str) -> sqlite3.Row | None:
    """Bookmark a normalised URL belongs to, following merges to the survivor.

    A live bookmark wins over a deleted one with the same URL.
    """
    row = conn.execute(
        "SELECT * FROM bookmarks WHERE url_normalized = ? "
        "ORDER BY deleted_at IS NOT NULL, created_at LIMIT 1",
        (url_normalized,),
    ).fetchone()
    return _survivor(conn, row)


def _fill_saved_at(conn, report, bookmark, saved_at, saved_source) -> None:
    """Fill an empty save date. Never replaces one, and is journaled."""
    if not saved_at or bookmark["saved_at"] is not None or bookmark["deleted_at"] is not None:
        return
    now = now_iso()
    conn.execute(
        "UPDATE bookmarks SET saved_at = ?, saved_source = ?, updated_at = ? WHERE id = ?",
        (saved_at, saved_source, now, bookmark["id"]),
    )
    conn.execute(
        "INSERT INTO changes (entity, entity_id, field, old_value, new_value, applied_at) "
        "VALUES ('bookmark', ?, 'saved_at', NULL, ?, ?)",
        (bookmark["id"], saved_at, now),
    )
    report.filled_saved_at += 1


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
    external_id: str | None = None,
    external_edited_at: str | None = None,
    saved_at: str | None = None,
    saved_source: str | None = None,
    extra: dict | None = None,
) -> None:
    """Apply the import rules to one record. Raises ValueError on a bad URL.

    ``extra`` gives curated fields a new bookmark starts with (platform,
    publication date), for records that already carry them.

    ``saved_at`` is a save time known from the source itself (a Notion page's
    creation time). Without it, a date with a time of day is used.
    """
    url_normalized = normalize_url(url)
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    digest = content_hash(url, title, summary, tags, date_text)

    def store(bookmark_id: str, pending: bool) -> None:
        conn.execute(
            "INSERT INTO source_records (bookmark_id, source, external_id, fetched_at, "
            "external_edited_at, raw, content_hash, pending) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bookmark_id,
                source,
                external_id,
                now_iso(),
                external_edited_at,
                raw_json,
                digest,
                int(pending),
            ),
        )

    # 1. A record we already hold, recognised by its external ID.
    if external_id is not None:
        record = conn.execute(
            "SELECT * FROM source_records WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        if record is not None:
            if record["content_hash"] == digest:
                report.unchanged += 1
                return
            bookmark = _survivor(
                conn,
                conn.execute(
                    "SELECT * FROM bookmarks WHERE id = ?", (record["bookmark_id"],)
                ).fetchone(),
            )
            deleted = bookmark["deleted_at"] is not None
            conn.execute(
                "UPDATE source_records SET bookmark_id = ?, raw = ?, content_hash = ?, "
                "fetched_at = ?, external_edited_at = ?, pending = ? WHERE id = ?",
                (
                    bookmark["id"],
                    raw_json,
                    digest,
                    now_iso(),
                    external_edited_at,
                    int(not deleted),
                    record["id"],
                ),
            )
            if deleted:
                report.tombstoned += 1
            else:
                report.pending += 1
            return

    # 2. A link we already hold.
    bookmark = find_bookmark(conn, url_normalized)
    if bookmark is not None:
        if source == "atlas":
            saved_at = None  # a shared copy says nothing about when *this* user saved it
        known = conn.execute(
            "SELECT 1 FROM source_records WHERE bookmark_id = ? AND content_hash = ?",
            (bookmark["id"], digest),
        ).fetchone()
        if known and external_id is None:
            report.unchanged += 1
            return
        if bookmark["deleted_at"] is not None:
            store(bookmark["id"], pending=False)
            report.tombstoned += 1
        elif known:
            # Same content from a new source, such as the Notion page behind
            # a row first imported from CSV: attach it, nothing to review.
            store(bookmark["id"], pending=False)
            report.linked += 1
        else:
            store(bookmark["id"], pending=True)
            report.pending += 1
        _fill_saved_at(conn, report, bookmark, saved_at, saved_source)
        return

    # 3. A new link.
    parsed = parse_date(date_text)
    if parsed is None:
        if date_text and date_text.strip():
            report.unreadable_dates += 1
    elif parsed.date_only:
        # Could be a save date or a publication date: the split-dates
        # grooming script decides, from the source record.
        report.date_only += 1
    elif saved_at is None:
        saved_at, saved_source = parsed.iso, "date_column"

    bookmark_id = uuid.uuid4().hex
    now = now_iso()
    conn.execute(
        "INSERT INTO bookmarks (id, url, url_normalized, domain, title, summary, "
        "saved_at, saved_source, platform, published_at, published_source, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bookmark_id,
            url.strip(),
            url_normalized,
            domain_of(url),
            (title or "").strip() or None,
            (summary or "").strip() or None,
            saved_at,
            saved_source if saved_at else None,
            (extra or {}).get("platform") or None,
            (extra or {}).get("published_at") or None,
            (extra or {}).get("published_source") or None,
            now,
            now,
        ),
    )
    for name in tags:
        conn.execute(
            "INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)",
            (bookmark_id, _tag_id(conn, name)),
        )
    store(bookmark_id, pending=False)
    report.created += 1


def import_csv(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    columns: dict[str, str] | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import a CSV export. All rows succeed together or nothing is written."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return import_csv_stream(conn, handle, columns=columns, dry_run=dry_run, name=str(path))


def import_csv_stream(
    conn: sqlite3.Connection,
    handle,
    *,
    columns: dict[str, str] | None = None,
    dry_run: bool = False,
    name: str = "the file",
) -> ImportReport:
    """Import CSV text from an open stream, under the same rules as a file."""
    report = ImportReport(dry_run=dry_run)
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError(f"{name} has no header row.")
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
                    tags=split_tags(row.get(mapping.get("tags", ""))),
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


# ---- bookmarks exported by a browser ---------------------------------------

# Top-level folders every browser creates; they say nothing about the content.
BROWSER_ROOTS = {
    "bookmarks bar", "bookmarks toolbar", "bookmarks menu", "other bookmarks", "mobile bookmarks",
    "favorites", "favourites", "favorites bar", "favourites bar", "imported", "unsorted bookmarks",
    "barre de favoris", "barre personnelle", "menu des marque-pages", "autres favoris",
    "autres marque-pages", "favoris", "marque-pages", "barre d'outils de marque-pages",
}  # fmt: skip


def looks_like_browser_export(text: str) -> bool:
    head = text[:3000].lower()
    return "netscape-bookmark-file" in head or ("<dl>" in head and "<dt>" in head and "<a " in head)


def read_browser_bookmarks(text: str) -> list[dict]:
    """The links of a bookmarks file exported by Chrome, Firefox, Safari or
    Edge (they all write the same old "Netscape" format). Each link comes with
    the names of the folders it sits in and the date it was added."""
    from html.parser import HTMLParser

    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.folders: list[str | None] = []
            self.pending = None  # a folder name read, waiting for its list to open
            self.capture = None  # "folder" or "link" while reading their text
            self.text = ""
            self.link = None
            self.items: list[dict] = []

        def handle_starttag(self, tag, attrs):
            values = {name.lower(): value or "" for name, value in attrs}
            if tag == "h3":
                self.capture, self.text = "folder", ""
            elif tag == "dl":
                self.folders.append(self.pending)
                self.pending = None
            elif tag == "a" and values.get("href"):
                self.capture, self.text = "link", ""
                self.link = {"url": values["href"].strip(), "added": values.get("add_date", "")}

        def handle_endtag(self, tag):
            if tag == "h3" and self.capture == "folder":
                self.pending, self.capture = " ".join(self.text.split()), None
            elif tag == "dl" and self.folders:
                self.folders.pop()
            elif tag == "a" and self.capture == "link":
                self.capture = None
                folders = [f for f in self.folders if f and f.lower() not in BROWSER_ROOTS]
                self.items.append(
                    {**self.link, "title": " ".join(self.text.split()), "folders": folders}
                )

        def handle_data(self, data):
            if self.capture:
                self.text += data

    reader = Reader()
    reader.feed(text)
    return reader.items


def import_browser_bookmarks(
    conn: sqlite3.Connection, text: str, *, dry_run: bool = False
) -> ImportReport:
    """Import a browser's bookmarks file. Folder names become tags, and the day
    a bookmark was added in the browser becomes its save date."""
    from datetime import UTC, datetime

    report = ImportReport(dry_run=dry_run)
    try:
        for number, item in enumerate(read_browser_bookmarks(text), start=1):
            report.rows += 1
            saved_at = None
            if item["added"].isdigit() and 0 < int(item["added"]) < 4_102_444_800:  # before 2100
                moment = datetime.fromtimestamp(int(item["added"]), tz=UTC)
                saved_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                import_record(
                    conn,
                    report,
                    source="browser",
                    raw=item,
                    url=item["url"],
                    title=item["title"] or None,
                    summary=None,
                    tags=item["folders"],
                    date_text=None,
                    saved_at=saved_at,
                    saved_source="browser" if saved_at else None,
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
