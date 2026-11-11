"""Bundles: a set of bookmarks as one file, to hand to someone else.

A bundle is plain JSON holding curated fields only: link, title, summary,
tags, platform and publication date. It carries no Notion identifiers, no
source records, no journal and no token. Importing one follows the same
rules as any import: new links become bookmarks, links already present are
kept aside for review and nothing is overwritten.
"""

from __future__ import annotations

import sqlite3

from atlas import __version__
from atlas.dates import parse_date
from atlas.db import now_iso
from atlas.importer import ImportReport, import_record

FORMAT = "bookmark-atlas-bundle"
VERSION = 1
MAX_BOOKMARKS = 50_000
_LIMITS = {"url": 4000, "title": 1000, "summary": 20_000, "platform": 80}


def export_bundle(conn: sqlite3.Connection, ids: list[str] | None = None) -> dict:
    """The live bookmarks (all, or those in ``ids``) as a bundle."""
    wanted = None if ids is None else set(ids)
    tags: dict[str, list[str]] = {}
    for bookmark_id, name in conn.execute(
        "SELECT bookmark_id, name FROM bookmark_tags JOIN tags ON tags.id = tag_id "
        "ORDER BY name COLLATE NOCASE"
    ):
        tags.setdefault(bookmark_id, []).append(name)
    items = []
    for row in conn.execute(
        "SELECT * FROM bookmarks WHERE deleted_at IS NULL "
        "ORDER BY COALESCE(saved_at, created_at), id"
    ):
        if wanted is not None and row["id"] not in wanted:
            continue
        items.append(
            {
                "url": row["url"],
                "title": row["title"],
                "summary": row["summary"],
                "tags": tags.get(row["id"], []),
                "platform": row["platform"],
                "published_at": row["published_at"],
                "published_source": row["published_source"],
            }
        )
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now_iso(),
        "app_version": __version__,
        "count": len(items),
        "bookmarks": items,
    }


def is_bundle(data) -> bool:
    return isinstance(data, dict) and data.get("format") == FORMAT


def _text(item: dict, key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    if len(value) > _LIMITS[key]:
        raise ValueError(f"{key} is too long")
    return value


def import_bundle(conn: sqlite3.Connection, data, *, dry_run: bool = False) -> ImportReport:
    """Import a bundle. All of it or nothing; entries that cannot be read are skipped."""
    if not is_bundle(data):
        raise ValueError("This file is not a Bookmark Atlas bundle.")
    if data.get("version") != VERSION:
        raise ValueError(
            f"This bundle uses format version {data.get('version')!r}; "
            f"this app reads version {VERSION}. Update the app."
        )
    items = data.get("bookmarks")
    if not isinstance(items, list):
        raise ValueError("The bundle has no list of bookmarks.")
    if len(items) > MAX_BOOKMARKS:
        raise ValueError(f"A bundle is limited to {MAX_BOOKMARKS} bookmarks.")

    report = ImportReport(dry_run=dry_run)
    received = now_iso()
    try:
        for number, item in enumerate(items, start=1):
            report.rows += 1
            try:
                if not isinstance(item, dict):
                    raise ValueError("not an object")
                url = _text(item, "url")
                if not url or not url.strip():
                    raise ValueError("no URL")
                tags = item.get("tags") or []
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    raise ValueError("tags must be a list of text")
                tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))[:200]
                published = _text({"platform": item.get("published_at")}, "platform")
                parsed = parse_date(published)
                clean = {
                    "url": url.strip(),
                    "title": _text(item, "title"),
                    "summary": _text(item, "summary"),
                    "tags": tags,
                    "platform": _text(item, "platform"),
                    "published_at": parsed.iso if parsed else None,
                }
                import_record(
                    conn,
                    report,
                    source="atlas",
                    raw=clean,
                    url=clean["url"],
                    title=clean["title"],
                    summary=clean["summary"],
                    tags=tags,
                    date_text=None,
                    # The sender's save date is theirs. For the receiver, the
                    # bookmark enters the collection now.
                    saved_at=received,
                    saved_source="shared_import",
                    extra={
                        "platform": clean["platform"],
                        "published_at": clean["published_at"],
                        "published_source": "shared_import" if clean["published_at"] else None,
                    },
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
