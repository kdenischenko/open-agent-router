"""Window durations and their boundaries. Pure functions."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from agent_router.clock import to_utc

_DURATION = re.compile(r"(\d+)(h|d|mo)")
_SPAN = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")
_TIME = r"(?:[ T](\d{1,2}):(\d{2}))?"
_DAY_FIRST = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})" + _TIME)
_YEAR_FIRST = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})" + _TIME)
_MOMENT_ERROR = (
    "expected a date such as 2026-12-01 or 01.12.2026, optionally with a time such as 14:00"
)


def parse_moment(text: str, tz: tzinfo | None = None) -> datetime:
    """Parse a date with an optional time, e.g. "2026-12-01 14:00" or "01.12.2026".

    A value without an offset is local time: in tz when given, else in the system time zone.
    A date alone means midnight. Full ISO 8601 with an offset is accepted as is.
    """
    value = " ".join(text.split()) if isinstance(text, str) else ""
    try:
        if match := _DAY_FIRST.fullmatch(value):
            day, month, year, hour, minute = match.groups()
            moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        elif match := _YEAR_FIRST.fullmatch(value):
            year, month, day, hour, minute = match.groups()
            moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        else:
            moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date {text!r}: {_MOMENT_ERROR}") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=tz) if tz is not None else moment.astimezone()
    return moment


@dataclass(frozen=True)
class Duration:
    hours: int = 0
    days: int = 0
    months: int = 0

    def __str__(self) -> str:
        if self.months:
            return f"{self.months}mo"
        if self.days:
            return f"{self.days}d"
        return f"{self.hours}h"


def parse_duration(text: str) -> Duration:
    """Parse a duration like "5h", "7d", or "1mo"."""
    match = _DURATION.fullmatch(text) if isinstance(text, str) else None
    if match is None or int(match.group(1)) <= 0:
        raise ValueError(f"invalid duration: {text!r} (expected 5h, 7d, or 1mo)")
    number, unit = int(match.group(1)), match.group(2)
    if unit == "h":
        return Duration(hours=number)
    if unit == "d":
        return Duration(days=number)
    return Duration(months=number)


def parse_span(text: str) -> timedelta:
    """Parse a span like "3h20m", "45m", or "2d"."""
    match = _SPAN.fullmatch(text) if isinstance(text, str) and text else None
    if match is None:
        raise ValueError(f"invalid span: {text!r} (expected 3h20m, 45m, or 2d)")
    days, hours, minutes = (int(part) if part else 0 for part in match.groups())
    span = timedelta(days=days, hours=hours, minutes=minutes)
    if span <= timedelta(0):
        raise ValueError(f"invalid span: {text!r} (must be positive)")
    return span


def _check(d: Duration) -> None:
    if d.hours < 0 or d.days < 0 or d.months < 0 or not (d.hours or d.days or d.months):
        raise ValueError("duration must be positive")


def shift(anchor: datetime, d: Duration, k: int) -> datetime:
    """anchor + k*d.

    Months are calendar-based: the day clamps to the end of the month,
    counted from the anchor.
    """
    base = to_utc(anchor)
    _check(d)
    if d.months:
        index = base.year * 12 + (base.month - 1) + k * d.months
        year, month = divmod(index, 12)
        day = min(base.day, calendar.monthrange(year, month + 1)[1])
        base = base.replace(year=year, month=month + 1, day=day)
    return base + timedelta(hours=k * d.hours, days=k * d.days)


def approx_length(d: Duration) -> timedelta:
    """Approximate length (month = 30 days): for rough estimates and age comparison only."""
    return timedelta(hours=d.hours, days=d.days + 30 * d.months)


def window_bounds(anchor: datetime, d: Duration, now: datetime) -> tuple[datetime, datetime]:
    """Bounds of the window containing now: start <= now < end.

    The reset moment already belongs to the next window.
    """
    base, moment = to_utc(anchor), to_utc(now)
    _check(d)
    k = (moment - base) // approx_length(d)
    while shift(base, d, k) > moment:
        k -= 1
    while shift(base, d, k + 1) <= moment:
        k += 1
    return shift(base, d, k), shift(base, d, k + 1)
