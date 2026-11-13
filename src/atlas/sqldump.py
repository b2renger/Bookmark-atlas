"""Read bookmarks out of a SQL dump (the .sql file phpMyAdmin or mysqldump
writes for a MySQL or MariaDB database).

The file is read as text and never run. Only what a bookmark is made of is
taken: link, title, description, the day it was added and its tags. Accounts,
authors, e-mail addresses, passwords and everything else in the dump are left
where they are, and are not copied into the collection, not even into the
record kept of what was read.

Old dumps come with their own damage, which is repaired on the way in:

- accents stored in one encoding and written out in another ("ScÃ©nographie");
- the same link saved several times: one bookmark, the earliest date, the
  fullest text, all the tags;
- text cut at the width of its column: a description gets an ellipsis, a link
  gets the tag ``TRUNCATED_TAG`` since it very likely no longer opens;
- tags used a single time in the whole dump, which tie nothing together, are
  left out (the record of what was read keeps them).
"""

from __future__ import annotations

import html
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .importer import ImportReport, import_record
from .urls import normalize_url

TRUNCATED_TAG = "link cut short"
SOURCE = "sqldump"

_INSERT = re.compile(r"INSERT INTO `?([A-Za-z0-9_]+)`? \(([^)]*)\) VALUES\s*")
_CREATE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?`?([A-Za-z0-9_]+)`? \((.*?)\n\)", re.S)
_WIDTH = re.compile(r"^\s*`([A-Za-z0-9_]+)`\s+(?:var)?char\((\d+)\)", re.M | re.I)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "", "Z": "", "b": ""}


def looks_like_sql_dump(text: str) -> bool:
    head = text[:200_000]
    return "INSERT INTO" in head and "CREATE TABLE" in head


def read_tables(text: str) -> dict[str, list[dict]]:
    """Every row of every ``INSERT`` of the dump, by table. Values are text,
    whole numbers or ``None``."""
    tables: dict[str, list[dict]] = {}
    size = len(text)
    for match in _INSERT.finditer(text):
        columns = [name.strip(" `") for name in match.group(2).split(",")]
        rows = tables.setdefault(match.group(1), [])
        at = match.end()
        while at < size:
            while at < size and text[at] in " \r\n\t,":
                at += 1
            if at >= size or text[at] != "(":
                break
            at += 1
            values: list = []
            while True:
                if text[at] == "'":
                    at += 1
                    parts = []
                    while True:
                        stop = at
                        while text[stop] not in "'\\":
                            stop += 1
                        parts.append(text[at:stop])
                        if text[stop] == "\\":
                            parts.append(_ESCAPES.get(text[stop + 1], text[stop + 1]))
                            at = stop + 2
                        elif text[stop + 1 : stop + 2] == "'":
                            parts.append("'")
                            at = stop + 2
                        else:
                            at = stop + 1
                            break
                    values.append("".join(parts))
                else:
                    stop = at
                    while text[stop] not in ",)":
                        stop += 1
                    word = text[at:stop].strip()
                    if word.upper() == "NULL":
                        values.append(None)
                    elif re.fullmatch(r"-?\d+", word):
                        values.append(int(word))
                    else:
                        values.append(word)
                    at = stop
                while text[at] == " ":
                    at += 1
                if text[at] == ",":
                    at += 1
                    while text[at] == " ":
                        at += 1
                    continue
                at += 1  # the closing bracket
                break
            if len(values) == len(columns):
                rows.append(dict(zip(columns, values, strict=True)))
            if text[at : at + 1] == ";":
                break
    return tables


def column_widths(text: str) -> dict[str, dict[str, int]]:
    """The declared width of each text column, by table."""
    return {
        match.group(1): {name: int(width) for name, width in _WIDTH.findall(match.group(2))}
        for match in _CREATE.finditer(text)
    }


def _byte(char: str) -> int | None:
    """The byte a character stood for before the text was misread."""
    code = ord(char)
    if code < 0x100:
        return code
    try:
        return char.encode("cp1252")[0]
    except UnicodeEncodeError:
        return None


def repair_text(text: str | None) -> str:
    """Undo accents written in one encoding and read in another: "Ã©" back to
    "é", "â€™" back to "’". Only the runs of characters that form a proper
    sequence are touched, so text that was right stays as it is, and a
    sequence cut in half at the end of a column is dropped."""
    if not text:
        return ""
    for _ in range(3):  # some old text was misread more than once
        if not re.search("[\u00c2-\u00f4]", text):
            break
        out, at, size, changed = [], 0, len(text), False
        while at < size:
            lead = _byte(text[at])
            need = (
                0
                if lead is None or ord(text[at]) < 0x80
                else 1
                if 0xC2 <= lead <= 0xDF
                else 2
                if 0xE0 <= lead <= 0xEF
                else 3
                if 0xF0 <= lead <= 0xF4
                else 0
            )
            if need:
                tail = [_byte(char) for char in text[at + 1 : at + 1 + need]]
                if len(tail) == need and all(b is not None and 0x80 <= b <= 0xBF for b in tail):
                    try:
                        out.append(bytes([lead, *tail]).decode("utf-8"))
                        at += need + 1
                        changed = True
                        continue
                    except UnicodeDecodeError:
                        pass
                elif (
                    changed  # only in text that was broken elsewhere: "café" must keep its end
                    and at + 1 + len(tail) >= size
                    and all(b is not None and 0x80 <= b <= 0xBF for b in tail)
                ):
                    break  # half a character at the very end: cut with the column
            out.append(text[at])
            at += 1
        text = "".join(out)
        if not changed:
            break
    return text


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_MARKUP = re.compile(r"</?[a-zA-Z][^<>]{0,200}>")


def clean(text: str | None) -> str:
    """Repaired text on one line: entities resolved, markup and stray control
    characters removed, and e-mail addresses taken out."""
    text = repair_text(text)
    text = re.sub(r"Ã (?=\w)", "à ", text)  # the second half of "à" had become a plain space
    for _ in range(4):  # "&amp;amp;#39;" needs several passes
        resolved = html.unescape(text)
        if resolved == text:
            break
        text = resolved
    # Quotes that were escaped once too often were stored as backslashes.
    text = re.sub(r"\\+(?=['\"’])", "", text)  # the quote is still there: drop the backslash
    text = re.sub(r"(\\+r)?\\+n(?=\\|\s|$)|\\+r\\+n", " ", text)  # a line break written out
    text = re.sub(r"(?<=\w)\\+ ?(?=\w)", "’", text)  # l\'impression, L\\ impression
    text = re.sub(r"\\+", '"', text)
    text = _MARKUP.sub(" ", _CONTROL.sub(" ", text))
    text = _EMAIL.sub("[address removed]", text)
    return " ".join(text.replace("\u00a0", " ").split())


def _pick(columns: list[str], *patterns: str) -> str | None:
    for pattern in patterns:
        for name in columns:
            if re.search(pattern, name, re.I):
                return name
    return None


@dataclass
class DumpReading:
    """What a dump holds, ready to import, and what was done to it."""

    items: list[dict] = field(default_factory=list)
    table: str = ""
    rows: int = 0
    merged: int = 0  # rows that were another saving of a link already read
    repaired: int = 0  # bookmarks whose accents were repaired
    cut_links: int = 0
    cut_texts: int = 0
    no_title: int = 0
    tags_kept: int = 0
    tags_dropped: int = 0  # used once in the whole dump
    left_out: list[str] = field(default_factory=list)  # tables not read
    skipped: list[tuple[int | str, str]] = field(default_factory=list)
    first_day: str = ""
    last_day: str = ""


def read_dump(text: str) -> DumpReading:
    """Find the table of links in a dump and return its bookmarks."""
    tables = read_tables(text)
    widths = column_widths(text)
    candidates = [
        (len(rows), name)
        for name, rows in tables.items()
        if rows and _pick(list(rows[0]), r"(^|_)(url|link|href)$")
    ]
    if not candidates:
        raise ValueError(
            "No table of links was found in this file: it needs a table with a column "
            "named url or link."
        )
    table = max(candidates)[1]
    rows = tables[table]
    columns = list(rows[0])
    url_col = _pick(columns, r"(^|_)(url|link|href)$")
    title_col = _pick(columns, r"(^|_)title$", r"(^|_)name$")
    text_col = _pick(columns, r"(^|_)(desc|description|summary|excerpt)$", r"(^|_)(content|text)$")
    date_col = _pick(columns, r"date_?add", r"creat", r"(^|_)(date|time|timestamp)$")
    id_col = _pick(columns, r"^id$", r"^" + re.escape(table.rstrip("s")) + r"_id$", r"_id$")

    # Tags: a table of names whose own name starts with "tag", and a table of
    # pairs that ties its rows to the links. Other groupings are left out.
    tags_of: dict = {}
    used: Counter = Counter()
    read = {table}
    for name, tag_rows in tables.items():
        if not name.lower().startswith("tag") or not tag_rows or name == table:
            continue
        tag_columns = list(tag_rows[0])
        tag_id = _pick(tag_columns, r"^id$", r"_id$")
        tag_name = _pick(tag_columns, r"(^|_)(name|label|title)$")
        if not tag_id or not tag_name:
            continue
        names = {row[tag_id]: clean(row[tag_name]) for row in tag_rows}
        for other, pairs in tables.items():
            if other in (name, table) or not pairs or len(pairs[0]) != 2:
                continue
            first, second = list(pairs[0])
            if {first, second} != {id_col, tag_id} and not (
                tag_id in (first, second) and id_col in (first, second)
            ):
                continue
            read.update((name, other))
            for pair in pairs:
                label = names.get(pair[tag_id])
                if label and pair[id_col] is not None:
                    tags_of.setdefault(pair[id_col], []).append(label)

    reading = DumpReading(table=table, rows=len(rows))
    reading.left_out = sorted(name for name, found in tables.items() if found and name not in read)
    url_width = widths.get(table, {}).get(url_col or "", 0)
    text_width = widths.get(table, {}).get(text_col or "", 0)
    by_key: dict[str, dict] = {}
    for number, row in enumerate(rows, start=1):
        url = str(row.get(url_col) or "").strip()
        try:
            key = normalize_url(url)
            if _EMAIL.search(url.partition("?")[2]):
                raise ValueError("the link carries an e-mail address")
        except ValueError as error:
            reading.skipped.append((row.get(id_col, number) if id_col else number, str(error)))
            continue
        raw_title = str(row.get(title_col) or "") if title_col else ""
        raw_text = str(row.get(text_col) or "") if text_col else ""
        title, summary = clean(raw_title), clean(raw_text)
        fixed = repair_text(raw_title) != raw_title or repair_text(raw_text) != raw_text
        # A column is as wide in bytes as it says; text that fills it was cut.
        if text_width and len(raw_text.encode("cp1252", "replace")) >= text_width - 2:
            summary = summary.rstrip(" .,;:") + "…"
            reading.cut_texts += 1
        saved = None
        stamp = row.get(date_col) if date_col else None
        if isinstance(stamp, int) and 0 < stamp < 4_102_444_800:
            saved = datetime.fromtimestamp(stamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(stamp, str) and re.match(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d", stamp):
            saved = stamp[:10] + "T" + stamp[11:19] + "Z"
        tags = list(dict.fromkeys(tags_of.get(row.get(id_col), []))) if id_col else []
        cut = bool(url_width) and len(url) >= url_width
        item = by_key.get(key)
        if item is None:
            by_key[key] = {
                "id": row.get(id_col) if id_col else number,
                "ids": [row.get(id_col) if id_col else number],
                "url": url,
                "title": title,
                "summary": summary,
                "saved_at": saved,
                "tags": tags,
                "cut": cut,
                "fixed": fixed,
            }
            continue
        reading.merged += 1
        item["ids"].append(row.get(id_col) if id_col else number)
        if len(title) > len(item["title"]):
            item["title"] = title
        if len(summary) > len(item["summary"]):
            item["summary"] = summary
        if saved and (not item["saved_at"] or saved < item["saved_at"]):
            item["saved_at"] = saved
        item["tags"] = list(dict.fromkeys(item["tags"] + tags))
        item["fixed"] = item["fixed"] or fixed
        if item["cut"] and not cut:
            item["url"], item["cut"] = url, False

    for item in by_key.values():
        # The same tag under two spellings counts once; the commonest spelling is kept later.
        item["tags"] = list({tag.casefold(): tag for tag in item["tags"]}.values())
        used.update(tag.casefold() for tag in item["tags"])
    for item in by_key.values():
        item["all_tags"] = item["tags"]
        item["tags"] = [tag for tag in item["tags"] if used[tag.casefold()] > 1]
        if item["cut"]:
            item["tags"].append(TRUNCATED_TAG)
            reading.cut_links += 1
        reading.repaired += item["fixed"]
        reading.no_title += not item["title"]
    reading.tags_kept = sum(1 for count in used.values() if count > 1)
    reading.tags_dropped = sum(1 for count in used.values() if count == 1)
    reading.items = sorted(by_key.values(), key=lambda item: item["saved_at"] or "")
    days = [item["saved_at"][:10] for item in reading.items if item["saved_at"]]
    reading.first_day, reading.last_day = (days[0], days[-1]) if days else ("", "")
    return reading


def import_dump(
    conn: sqlite3.Connection, reading: DumpReading, *, dry_run: bool = False
) -> ImportReport:
    """Write the bookmarks of a dump into the collection."""
    report = ImportReport(dry_run=dry_run)
    report.skipped.extend(reading.skipped)
    try:
        for item in reading.items:
            report.rows += 1
            try:
                import_record(
                    conn,
                    report,
                    source=SOURCE,
                    # What is kept of the original: nothing about who saved it.
                    raw={
                        "table": reading.table,
                        "ids": item["ids"],
                        "url": item["url"],
                        "title": item["title"],
                        "summary": item["summary"],
                        "tags": item["all_tags"],
                    },
                    url=item["url"],
                    title=item["title"] or None,
                    summary=item["summary"] or None,
                    tags=item["tags"],
                    date_text=None,
                    external_id=f"{reading.table}:{item['id']}",
                    saved_at=item["saved_at"],
                    saved_source=SOURCE if item["saved_at"] else None,
                )
            except ValueError as error:
                report.skipped.append((item["id"], str(error)))
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report


def describe(reading: DumpReading) -> dict:
    """The reading as plain figures, for the page and the command line."""
    return {
        "table": reading.table,
        "rows": reading.rows,
        "bookmarks": len(reading.items),
        "merged": reading.merged,
        "repaired": reading.repaired,
        "cut_links": reading.cut_links,
        "cut_texts": reading.cut_texts,
        "no_title": reading.no_title,
        "tags_kept": reading.tags_kept,
        "tags_dropped": reading.tags_dropped,
        "left_out": reading.left_out,
        "first_day": reading.first_day,
        "last_day": reading.last_day,
        "skipped": len(reading.skipped),
    }


def into_workspace(home_path, text: str, name: str) -> dict:
    """Read a dump and put its bookmarks in a new workspace of their own, so a
    large old collection does not pour into the one that is open."""
    from . import db, workspaces

    reading = read_dump(text)
    if not reading.items:
        raise ValueError("The table of links in this file is empty.")
    made = workspaces.create(None, home_path, name, empty=True, started_from="a database dump")
    target = workspaces.path_of(home_path, made["id"])
    try:
        conn = db.open_ready(target)
        try:
            report = import_dump(conn, reading)
        finally:
            conn.close()
    except BaseException:
        workspaces.delete(home_path, made["id"])
        raise
    made["bookmarks"] = report.created
    return {"workspace": made, "reading": describe(reading), "report": report}
