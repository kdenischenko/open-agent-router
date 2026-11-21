"""System and fake clocks. Values are stored in UTC and shown in local time."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol


def to_utc(moment: datetime) -> datetime:
    """Converts a moment to UTC; naive datetimes are rejected."""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime must have a timezone")
    return moment.astimezone(UTC)


def format_local(moment: datetime | str | None, tz: tzinfo | None = None) -> str:
    """Formats a moment in the given or local timezone; an empty value yields a dash."""
    if moment is None or moment == "":
        return "—"
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return to_utc(moment).astimezone(tz).strftime("%d.%m %H:%M")


def format_age(seconds: float | None) -> str:
    """Age in the two largest units: 42 s, 2 min, 2 h 5 min, 2 d 7 h; no value yields a dash."""
    if seconds is None:
        return "—"
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole} s"
    minutes = whole // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Fake clock for tests: time stands still until it is advanced."""

    def __init__(self, start: datetime) -> None:
        self._now = to_utc(start)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, moment: datetime) -> None:
        value = to_utc(moment)
        with self._lock:
            self._now = value

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            self._now = self._now + delta
