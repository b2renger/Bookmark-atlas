import pytest

from atlas.dates import parse_date


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("December 29, 2025 7:49 PM (GMT+1)", "2025-12-29T18:49:00Z"),
        ("June 14, 2026 9:30 AM (GMT+2)", "2026-06-14T07:30:00Z"),
        ("January 1, 2026 12:30 AM (GMT+1)", "2025-12-31T23:30:00Z"),
        ("July 4, 2026 12:15 PM (GMT-3)", "2026-07-04T15:15:00Z"),
        ("July 4, 2026 6:00 PM (GMT+5:30)", "2026-07-04T12:30:00Z"),
        ("July 4, 2026 6:00 PM (UTC)", "2026-07-04T18:00:00Z"),
        ("2026-07-04T18:00:00+02:00", "2026-07-04T16:00:00Z"),
        ("2026-07-04T18:00:00Z", "2026-07-04T18:00:00Z"),
    ],
)
def test_dates_with_a_time_become_utc(text, iso):
    parsed = parse_date(text)
    assert parsed.iso == iso
    assert not parsed.date_only


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("June 14, 2026 2:00 AM (GMT+2)", "2026-06-14"),  # midnight UTC
        ("January 9, 2026 1:00 AM (GMT+1)", "2026-01-09"),
        ("January 9, 2026", "2026-01-09"),
        ("2026-01-09", "2026-01-09"),
        ("2026-01-09T00:00:00Z", "2026-01-09"),
    ],
)
def test_dates_without_a_time_are_flagged(text, day):
    parsed = parse_date(text)
    assert parsed.iso == day
    assert parsed.date_only


def test_range_reads_its_start():
    assert parse_date("May 1, 2026 10:00 AM (GMT+2) → May 3, 2026").iso == "2026-05-01T08:00:00Z"


@pytest.mark.parametrize("text", [None, "", "  ", "soon", "Smarch 3, 2026", "February 30, 2026"])
def test_unreadable_dates_return_none(text):
    assert parse_date(text) is None
