"""Tendencies over time: when things were saved and published, how fast they
were saved after publication, and which topics and tags are rising.

Standard library only. Everything is counted back from the latest date found
in the collection, so the figures do not change just because time passes.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta
from statistics import median

MONTHS_SHOWN = 36
RECENT_DAYS = 90

GAPS = (
    ("same day", 0, 0),
    ("within a week", 1, 7),
    ("within a month", 8, 30),
    ("within a year", 31, 365),
    ("more than a year later", 366, 10**6),
)


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _month_range(first: date, last: date) -> list[str]:
    months, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _series(days: list[date], months: list[str]) -> dict:
    counts = Counter(d.strftime("%Y-%m") for d in days)
    shown = [{"month": month, "count": counts.get(month, 0)} for month in months]
    return {
        "months": shown,
        "total": len(days),
        "earlier": sum(count for month, count in counts.items() if months and month < months[0]),
    }


def _movement(recent: Counter, before: Counter, names: dict, window: dict, minimum: int):
    """Share of the recent bookmarks against share of the earlier ones."""
    moves = []
    for key, name in names.items():
        now, then = recent.get(key, 0), before.get(key, 0)
        if now + then < minimum:
            continue
        share_now = now / window["recent"] if window["recent"] else 0.0
        share_then = then / window["before"] if window["before"] else 0.0
        moves.append(
            {
                "key": key,
                "name": name,
                "recent": now,
                "before": then,
                "share_recent": round(share_now, 4),
                "share_before": round(share_then, 4),
                "change": round(share_now - share_then, 4),
            }
        )
    moves.sort(key=lambda item: (-item["change"], item["name"].lower()))
    return moves


def overview(
    conn: sqlite3.Connection, run_id: str | None = None, topic_names: dict | None = None
) -> dict:
    """Everything the Trends tab shows. ``topic_names`` maps cluster numbers of
    ``run_id`` to their labels; without a run, topics are left out."""
    rows = conn.execute(
        "SELECT id, saved_at, published_at FROM bookmarks WHERE deleted_at IS NULL"
    ).fetchall()
    saved = {row["id"]: _day(row["saved_at"]) for row in rows}
    published = {row["id"]: _day(row["published_at"]) for row in rows}
    saved_days = [d for d in saved.values() if d]
    published_days = [d for d in published.values() if d]
    coverage = {
        "bookmarks": len(rows),
        "saved": len(saved_days),
        "published": len(published_days),
        "both": sum(1 for row in rows if saved[row["id"]] and published[row["id"]]),
        "neither": sum(1 for row in rows if not saved[row["id"]] and not published[row["id"]]),
    }
    result = {"coverage": coverage, "latest": None, "saved": None, "published": None}
    every = saved_days + published_days
    if not every:
        return {**result, "reaction": None, "topics": None, "tags": None}
    latest = max(every)
    months = _month_range(min(every), latest)[-MONTHS_SHOWN:]
    result.update(
        latest=latest.isoformat(),
        saved=_series(saved_days, months),
        published=_series(published_days, months),
    )

    # Reaction time: days between publication and saving.
    gaps = [
        (saved[row["id"]] - published[row["id"]]).days
        for row in rows
        if saved[row["id"]] and published[row["id"]]
    ]
    usable = [gap for gap in gaps if gap >= 0]
    result["reaction"] = {
        "measured": len(usable),
        "saved_before_published": len(gaps) - len(usable),
        "median_days": median(usable) if usable else None,
        "buckets": [
            {"label": label, "count": sum(1 for gap in usable if low <= gap <= high)}
            for label, low, high in GAPS
        ],
    }

    # Rising and fading: the last 90 days against everything before.
    when = {row["id"]: saved[row["id"]] or published[row["id"]] for row in rows}
    cut = latest - timedelta(days=RECENT_DAYS)
    is_recent = {b: d > cut for b, d in when.items() if d}
    window = {
        "recent_days": RECENT_DAYS,
        "since": (cut + timedelta(days=1)).isoformat(),
        "recent": sum(is_recent.values()),
        "before": len(is_recent) - sum(is_recent.values()),
        "by_saved_date": sum(1 for b in is_recent if saved[b]),
        "by_published_date": sum(1 for b in is_recent if not saved[b]),
    }
    result["window"] = window

    tag_recent, tag_before, tag_names = Counter(), Counter(), {}
    for bookmark_id, tag_id, name in conn.execute(
        "SELECT bt.bookmark_id, t.id, t.name FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id"
    ):
        if bookmark_id in is_recent:
            (tag_recent if is_recent[bookmark_id] else tag_before)[tag_id] += 1
            tag_names[tag_id] = name
    # Shares are of bookmarks, not of tag uses.
    tag_moves = _movement(tag_recent, tag_before, tag_names, window, minimum=5)
    result["tags"] = {
        "rising": [m for m in tag_moves if m["change"] > 0 and m["recent"] >= 3][:12],
        "fading": [m for m in reversed(tag_moves) if m["change"] < 0 and m["before"] >= 5][:12],
    }

    result["topics"] = None
    if run_id is not None:
        topic_recent, topic_before = Counter(), Counter()
        for bookmark_id, number in conn.execute(
            "SELECT bookmark_id, cluster FROM run_items WHERE run_id = ?", (run_id,)
        ):
            if bookmark_id in is_recent:
                (topic_recent if is_recent[bookmark_id] else topic_before)[number] += 1
        names = {
            number: (topic_names or {}).get(number) or f"Topic {number}"
            for number in sorted(set(topic_recent) | set(topic_before))
        }
        result["topics"] = {
            "cluster_run": run_id,
            "moves": _movement(topic_recent, topic_before, names, window, minimum=1),
        }
    return result
