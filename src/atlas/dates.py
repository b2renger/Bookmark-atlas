"""Reading dates as they appear in Notion exports.

Notion's CSV export writes dates like ``December 29, 2025 7:49 PM (GMT+1)``.
A date stored without a time of day comes out as midnight UTC, which is how a
date-only value is recognised here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

_MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}

_NOTION = re.compile(
    r"""^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})
        (?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])
           (?:\s*\((?:GMT|UTC)(?P<sign>[+-])?(?P<off_h>\d{1,2})?(?::(?P<off_m>\d{2}))?\))?
        )?$""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class ParsedDate:
    iso: str  # ISO 8601 in UTC; a plain date when date_only is true
    date_only: bool


def _format(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(text: str | None) -> ParsedDate | None:
    """Parse a Notion export date or an ISO 8601 date. None if unreadable.

    A range (``start → end``) is read as its start.
    """
    if not text or not text.strip():
        return None
    text = text.split("→")[0].strip()

    match = _NOTION.match(text)
    if match:
        month = _MONTHS.get(match["month"].lower())
        if month is None:
            return None
        try:
            day = date(int(match["year"]), month, int(match["day"]))
        except ValueError:
            return None
        if match["hour"] is None:
            return ParsedDate(day.isoformat(), True)
        hour = int(match["hour"]) % 12 + (12 if match["ampm"].lower() == "pm" else 0)
        offset = timedelta(hours=int(match["off_h"] or 0), minutes=int(match["off_m"] or 0))
        if match["sign"] == "-":
            offset = -offset
        moment = datetime(day.year, day.month, day.day, hour, int(match["minute"]))
        return _from_moment(moment.replace(tzinfo=timezone(offset)))

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return ParsedDate(date.fromisoformat(text).isoformat(), True)
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _from_moment(moment)


def _from_moment(moment: datetime) -> ParsedDate:
    utc = moment.astimezone(UTC)
    if (utc.hour, utc.minute, utc.second) == (0, 0, 0):
        return ParsedDate(utc.date().isoformat(), True)
    return ParsedDate(_format(utc), False)
