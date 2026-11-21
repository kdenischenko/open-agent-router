from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_router.clock import FakeClock, SystemClock
from agent_router.periods import (
    Duration,
    parse_duration,
    parse_moment,
    parse_span,
    shift,
    window_bounds,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_parse_duration_units():
    assert parse_duration("5h") == Duration(hours=5)
    assert parse_duration("7d") == Duration(days=7)
    assert parse_duration("1mo") == Duration(months=1)


@pytest.mark.parametrize("text", ["", "5", "h", "5x", "0h", "-1d", "1.5h", "5 h", "1mo2d", None])
def test_parse_duration_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_span():
    assert parse_span("3h20m") == timedelta(hours=3, minutes=20)
    assert parse_span("45m") == timedelta(minutes=45)
    assert parse_span("2d") == timedelta(days=2)
    assert parse_span("1d2h3m") == timedelta(days=1, hours=2, minutes=3)


@pytest.mark.parametrize("text", ["", "abc", "20m3h", "0m", "3h 20m", "-5m", "5"])
def test_parse_span_garbage(text):
    with pytest.raises(ValueError):
        parse_span(text)


def test_window_bounds_hours():
    start, end = window_bounds(utc(2026, 11, 12, 10), Duration(hours=5), utc(2026, 11, 12, 23, 30))
    assert (start, end) == (utc(2026, 11, 12, 20), utc(2026, 11, 13, 1))


def test_window_bounds_anchor_in_future():
    start, end = window_bounds(utc(2026, 11, 13, 6), Duration(hours=5), utc(2026, 11, 12, 23, 30))
    assert (start, end) == (utc(2026, 11, 12, 20), utc(2026, 11, 13, 1))


def test_window_bounds_now_on_boundary_starts_new_window():
    start, end = window_bounds(utc(2026, 11, 12, 10), Duration(hours=5), utc(2026, 11, 12, 20))
    assert (start, end) == (utc(2026, 11, 12, 20), utc(2026, 11, 13, 1))


def test_window_bounds_days():
    start, end = window_bounds(utc(2026, 11, 2, 9), Duration(days=7), utc(2026, 11, 12, 8))
    assert (start, end) == (utc(2026, 11, 9, 9), utc(2026, 11, 16, 9))


def test_month_from_day_31_clamps_and_counts_from_anchor():
    anchor = utc(2026, 1, 31, 12)
    month = Duration(months=1)
    assert shift(anchor, month, 1) == utc(2026, 2, 28, 12)
    assert shift(anchor, month, 2) == utc(2026, 3, 31, 12)
    assert shift(anchor, month, 3) == utc(2026, 4, 30, 12)
    assert window_bounds(anchor, month, utc(2026, 2, 10)) == (anchor, utc(2026, 2, 28, 12))
    assert window_bounds(anchor, month, utc(2026, 3, 1)) == (
        utc(2026, 2, 28, 12),
        utc(2026, 3, 31, 12),
    )
    assert window_bounds(anchor, month, utc(2026, 4, 1)) == (
        utc(2026, 3, 31, 12),
        utc(2026, 4, 30, 12),
    )


def test_month_leap_year():
    anchor = utc(2028, 1, 31, 12)
    assert shift(anchor, Duration(months=1), 1) == utc(2028, 2, 29, 12)
    assert window_bounds(anchor, Duration(months=1), utc(2028, 2, 29, 12)) == (
        utc(2028, 2, 29, 12),
        utc(2028, 3, 31, 12),
    )


def test_month_backwards_and_across_years():
    anchor = utc(2026, 1, 31, 12)
    assert shift(anchor, Duration(months=1), -2) == utc(2025, 11, 30, 12)
    assert shift(anchor, Duration(months=1), 12) == utc(2027, 1, 31, 12)
    start, end = window_bounds(anchor, Duration(months=1), utc(2025, 12, 15))
    assert (start, end) == (utc(2025, 11, 30, 12), utc(2025, 12, 31, 12))


def test_naive_datetimes_rejected():
    naive = datetime(2026, 11, 12, 10)
    with pytest.raises(ValueError):
        window_bounds(naive, Duration(hours=5), utc(2026, 11, 12, 23))
    with pytest.raises(ValueError):
        window_bounds(utc(2026, 11, 12, 10), Duration(hours=5), naive)
    with pytest.raises(ValueError):
        shift(naive, Duration(hours=5), 1)


def test_other_timezones_are_converted_to_utc():
    msk = timezone(timedelta(hours=3))
    start, end = window_bounds(
        datetime(2026, 11, 12, 13, tzinfo=msk), Duration(hours=5), utc(2026, 11, 12, 23, 30)
    )
    assert (start, end) == (utc(2026, 11, 12, 20), utc(2026, 11, 13, 1))
    assert start.tzinfo is UTC


def test_zero_duration_rejected():
    with pytest.raises(ValueError):
        window_bounds(utc(2026, 1, 1), Duration(), utc(2026, 1, 2))


def test_fake_clock():
    clock = FakeClock(utc(2026, 11, 12, 10))
    assert clock.now() == utc(2026, 11, 12, 10)
    clock.advance(timedelta(minutes=30))
    assert clock.now() == utc(2026, 11, 12, 10, 30)
    clock.set(utc(2027, 1, 1))
    assert clock.now() == utc(2027, 1, 1)
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 1, 1))


def test_system_clock_is_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


MSK = timezone(timedelta(hours=3))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-12-01", datetime(2026, 12, 1, tzinfo=MSK)),
        ("2026-12-01 14:00", datetime(2026, 12, 1, 14, tzinfo=MSK)),
        ("2026-12-01T14:00", datetime(2026, 12, 1, 14, tzinfo=MSK)),
        ("01.12.2026", datetime(2026, 12, 1, tzinfo=MSK)),
        ("1.12.2026 9:05", datetime(2026, 12, 1, 9, 5, tzinfo=MSK)),
        (" 01.12.2026  14:00 ", datetime(2026, 12, 1, 14, tzinfo=MSK)),
        ("2026-12-01T14:00:00+00:00", datetime(2026, 12, 1, 14, tzinfo=UTC)),
    ],
)
def test_parse_moment_accepts_a_date_or_a_date_and_time(text: str, expected: datetime) -> None:
    moment = parse_moment(text, MSK)

    assert moment == expected
    assert moment.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize("text", ["", "tomorrow", "32.12.2026", "2026-13-01", "01.12.26"])
def test_parse_moment_rejects_other_text(text: str) -> None:
    with pytest.raises(ValueError, match="expected a date such as 2026-12-01 or 01.12.2026"):
        parse_moment(text, MSK)
