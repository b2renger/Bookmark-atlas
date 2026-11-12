"""Find when a bookmarked page was published, by reading the page itself.

Two steps, kept apart on purpose:

- ``lookup`` goes online. It fetches the pages of bookmarks that have no
  publication date, reads the date their publisher declares, and stores what
  it found in the ``page_facts`` table. It changes no bookmark.
- the grooming script ``published-from-page`` reads that table and proposes
  the dates, so they go through the usual preview, apply and undo.

Where a date is read from, most trusted first: structured data (JSON-LD
``datePublished``), then the meta tags publishers use for it, then a
``<time>`` element marked as the publication date. For a GitHub repository
the day the repository was created is used, from GitHub's public API.

Only the declared date is used. Nothing is guessed from the text.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from atlas.dates import parse_date
from atlas.db import now_iso
from atlas.urls import x_status_id

USER_AGENT = "BookmarkAtlas (personal bookmark manager; reads the publication date of saved pages)"
MAX_BYTES = 600_000
TIMEOUT = 12

# Meta tags that carry a publication date, most reliable first.
META = [
    ("property", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "article:published_time"),
    ("itemprop", "datepublished"),
    ("name", "citation_publication_date"),
    ("name", "citation_online_date"),
    ("name", "citation_date"),
    ("name", "dc.date.issued"),
    ("name", "dcterms.issued"),
    ("name", "dcterms.created"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("name", "pubdate"),
    ("name", "publishdate"),
    ("name", "publish-date"),
    ("name", "dc.date"),
    ("name", "date"),
    ("itemprop", "uploaddate"),
]
JSON_KEYS = ("datePublished", "uploadDate", "dateCreated")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[tuple[str, str], str] = {}
        self.json: list[str] = []
        self.times: list[str] = []
        self._in_json = False

    def handle_starttag(self, tag, attrs):
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            content = values.get("content", "").strip()
            for attribute in ("property", "name", "itemprop"):
                key = values.get(attribute, "").strip().lower()
                if key and content:
                    self.meta.setdefault((attribute, key), content)
        elif tag == "script" and "ld+json" in values.get("type", "").lower():
            self._in_json = True
            self.json.append("")
        elif tag == "time" and values.get("datetime"):
            if "pubdate" in values or values.get("itemprop", "").lower() == "datepublished":
                self.times.append(values["datetime"].strip())

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_json = False

    def handle_data(self, data):
        if self._in_json:
            self.json[-1] += data


def _in_json(node, depth=0):
    """The first publication date in a JSON-LD structure, by key priority."""
    if depth > 8:
        return None
    if isinstance(node, list):
        for item in node:
            found = _in_json(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    for key in JSON_KEYS:
        if isinstance(node.get(key), str) and node[key].strip():
            return key, node[key].strip()
    for value in node.values():
        if isinstance(value, dict | list):
            found = _in_json(value, depth + 1)
            if found:
                return found
    return None


def clean_date(text: str) -> str | None:
    """A date as stored (``2026-03-15`` or ``2026-03-15T09:30:00Z``), or None
    when it cannot be read or is not a believable publication date."""
    text = (text or "").strip()
    if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", text):
        year, month, day = text.split("/")
        text = f"{year}-{int(month):02d}-{int(day):02d}"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?.*", text):
        text = text.replace(" ", "T", 1)
    parsed = parse_date(text)
    if parsed is None:
        return None
    try:
        day = date.fromisoformat(parsed.iso[:10])
    except ValueError:
        return None
    if day.year < 1990 or day > datetime.now(UTC).date() + timedelta(days=1):
        return None
    return parsed.iso[:10] if parsed.date_only else parsed.iso


def extract_published(html: str) -> tuple[str, str] | None:
    """``(date, where it was read)`` from a page, or None."""
    page = _Page()
    try:
        page.feed(html)
    except Exception:  # a broken page still gives what was parsed so far
        pass
    for block in page.json:
        try:
            found = _in_json(json.loads(block))
        except ValueError:
            continue
        if found and clean_date(found[1]):
            return clean_date(found[1]), f"structured data ({found[0]})"
    for attribute, key in META:
        value = page.meta.get((attribute, key))
        if value and clean_date(value):
            return clean_date(value), f"meta tag {key}"
    for value in page.times:
        if clean_date(value):
            return clean_date(value), "time element"
    return None


def github_repository(url: str) -> str | None:
    """``owner/name`` when the link is a repository's own page."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2 or segments[0] in ("orgs", "topics", "search", "sponsors", "features"):
        return None
    return f"{segments[0]}/{segments[1].removesuffix('.git')}"


def refused(url: str) -> str | None:
    """Why a link is not fetched, or None when it may be."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "not a web link"
    host = (parts.hostname or "").lower()
    if not host or "." not in host or host.endswith((".local", ".internal", ".lan")):
        return "an address on a private network"
    try:
        if not ipaddress.ip_address(host).is_global:
            return "an address on a private network"
    except ValueError:
        pass
    if x_status_id(url):
        return "an X post: its date comes from the post ID script"
    return None


class Limited(Exception):
    """The site asked us to slow down."""


def http_get(url: str, accept: str = "text/html,application/xhtml+xml") -> tuple[int, str]:
    """Status and the start of the body. Follows redirects; reads at most MAX_BYTES."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read(MAX_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return response.status, raw.decode(charset, errors="replace")
            except LookupError:
                return response.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, ""


def read_one(url: str, get=None) -> dict:
    """Look one link up. Returns the columns of a ``page_facts`` row (without IDs).

    Raises ``Limited`` when GitHub says to come back later, so the caller can
    stop asking instead of recording a failure.
    """
    get = get or http_get
    why = refused(url)
    if why:
        return {"status": "skipped", "detail": why}
    try:
        repository = github_repository(url)
        if repository:
            status, body = get(
                f"https://api.github.com/repos/{repository}", "application/vnd.github+json"
            )
            if status in (403, 429):
                raise Limited
            if status != 200:
                return {"status": "error", "detail": f"GitHub answered {status}"}
            created = clean_date(str(json.loads(body).get("created_at") or ""))
            if not created:
                return {"status": "none", "detail": "GitHub gave no creation date"}
            return {"status": "found", "published_at": created, "found_in": "github"}
        status, body = get(url)
        if status != 200:
            return {"status": "error", "detail": f"the site answered {status}"}
        found = extract_published(body)
        if not found:
            return {"status": "none", "detail": "the page declares no publication date"}
        return {"status": "found", "published_at": found[0], "found_in": found[1]}
    except Limited:
        raise
    except Exception as error:  # timeouts, refused connections, bad certificates, bad JSON
        return {"status": "error", "detail": f"{type(error).__name__}: {error}"[:200]}


def _candidates(conn: sqlite3.Connection, *, skip_github: bool) -> list:
    rows = conn.execute(
        "SELECT b.id, b.url, f.status FROM bookmarks b LEFT JOIN page_facts f "
        "ON f.bookmark_id = b.id WHERE b.deleted_at IS NULL AND b.published_at IS NULL "
        "ORDER BY b.created_at, b.id"
    ).fetchall()
    wanted = []
    for row in rows:
        if row["status"] is not None:
            continue
        if skip_github and github_repository(row["url"]):
            continue
        wanted.append(row)
    return wanted


def status(conn: sqlite3.Connection) -> dict:
    """Where the lookup stands."""
    counts = dict(
        conn.execute(
            "SELECT f.status, COUNT(*) FROM page_facts f JOIN bookmarks b ON b.id = f.bookmark_id "
            "WHERE b.deleted_at IS NULL GROUP BY f.status"
        ).fetchall()
    )
    ready = conn.execute(
        "SELECT COUNT(*) FROM page_facts f JOIN bookmarks b ON b.id = f.bookmark_id "
        "WHERE b.deleted_at IS NULL AND b.published_at IS NULL AND f.status = 'found'"
    ).fetchone()[0]
    missing = conn.execute(
        "SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL AND published_at IS NULL"
    ).fetchone()[0]
    return {
        "without_date": missing,
        "to_look_up": len(_candidates(conn, skip_github=False)),
        "ready": ready,
        "found": counts.get("found", 0),
        "none": counts.get("none", 0),
        "error": counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
    }


def lookup(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    retry_failed: bool = False,
    skip_github: bool = False,
    get=None,
) -> dict:
    """Look up to ``limit`` bookmarks up and record what was found.

    Returns what happened in this batch and how many are left, so the caller
    can go on batch by batch and show progress. ``retry_failed`` first forgets
    the pages that could not be reached, so they are tried again (once: pass
    it on the first batch only).
    """
    limit = max(1, min(int(limit), 50))
    if retry_failed:
        conn.execute("DELETE FROM page_facts WHERE status = 'error'")
        conn.commit()
    batch = _candidates(conn, skip_github=skip_github)[:limit]
    done = {"found": 0, "none": 0, "error": 0, "skipped": 0}
    limited = False

    def work(row):
        try:
            return row, read_one(row["url"], get)
        except Limited:
            return row, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, batch))
    for row, fact in results:
        if fact is None:
            limited = True
            continue
        conn.execute(
            "INSERT INTO page_facts (bookmark_id, url, fetched_at, status, published_at, "
            "found_in, detail) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(bookmark_id) DO UPDATE SET "
            "url = excluded.url, fetched_at = excluded.fetched_at, status = excluded.status, "
            "published_at = excluded.published_at, found_in = excluded.found_in, "
            "detail = excluded.detail",
            (
                row["id"],
                row["url"],
                now_iso(),
                fact["status"],
                fact.get("published_at"),
                fact.get("found_in"),
                fact.get("detail"),
            ),
        )
        done[fact["status"]] += 1
    conn.commit()
    skip_github = skip_github or limited
    return {
        "looked_up": sum(done.values()),
        **done,
        "github_limited": limited,
        "remaining": len(_candidates(conn, skip_github=skip_github)),
        "status": status(conn),
    }
