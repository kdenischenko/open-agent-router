from __future__ import annotations

import calendar
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from agent_router.config import Window
from agent_router.ledger import WindowState
from agent_router.pacing import window_pace
from agent_router.periods import Duration, parse_duration, shift, window_bounds

WINDOW_SEED = 20261112
PACE_SEED = 20261113
CASE_COUNT = 200
DURATION_TEXTS = ("5h", "3d", "7d", "1mo")
DURATION_SPANS = {
    "5h": timedelta(hours=5),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "1mo": timedelta(days=30),
}


def _random_anchor(rng: random.Random, case: int) -> datetime:
    year = rng.randint(2020, 2030)
    month = rng.randint(1, 12)
    if case % 17 == 0:
        year = rng.choice((2020, 2024, 2028))
        month = 2
        day = 29
    elif case % 3 == 0:
        month_days = calendar.monthrange(year, month)[1]
        edge_days = tuple(day for day in (29, 30, 31) if day <= month_days)
        day = rng.choice(edge_days) if edge_days else 28
    else:
        day = rng.randint(1, calendar.monthrange(year, month)[1])
    return datetime(
        year,
        month,
        day,
        rng.randrange(24),
        rng.randrange(60),
        rng.randrange(60),
        tzinfo=UTC,
    )


def _window_cases(text: str) -> list:
    rng = random.Random(WINDOW_SEED)
    cases = []
    for case in range(CASE_COUNT):
        anchor = _random_anchor(rng, case)
        direction = 1 if case % 2 else -1
        offset = timedelta(seconds=rng.randint(1, 3 * 365 * 24 * 60 * 60))
        now = anchor + direction * offset
        case_id = (
            f"seed={WINDOW_SEED};case={case};anchor={anchor.isoformat()};"
            f"duration={text};now={now.isoformat()}"
        )
        cases.append(pytest.param(anchor, parse_duration(text), now, id=case_id))
    return cases


def _shift_index(anchor: datetime, duration: Duration, start: datetime) -> int:
    if duration.months:
        months = (start.year - anchor.year) * 12 + start.month - anchor.month
        assert months % duration.months == 0
        return months // duration.months
    span = timedelta(hours=duration.hours, days=duration.days)
    elapsed = start - anchor
    assert elapsed % span == timedelta(0)
    return elapsed // span


@pytest.mark.parametrize(
    ("anchor", "duration", "now"),
    [case for text in DURATION_TEXTS for case in _window_cases(text)],
)
def test_window_bounds_properties(anchor: datetime, duration: Duration, now: datetime):
    start, end = window_bounds(anchor, duration, now)

    assert start <= now < end
    assert start < end
    k = _shift_index(anchor, duration, start)
    assert start == shift(anchor, duration, k)
    assert end == shift(anchor, duration, k + 1)


def _pace_cases() -> list:
    rng = random.Random(PACE_SEED)
    cases = []
    for case in range(CASE_COUNT):
        text = DURATION_TEXTS[case % len(DURATION_TEXTS)]
        kind = ("fixed", "rolling")[case % 2]
        role = "guard" if case % 4 == 0 else "pace"
        window = Window(
            id=f"window-{case}",
            duration=parse_duration(text),
            kind=kind,
            role=role,
            anchor=None,
            limit_usd=None,
            block_at=1.0 if role == "pace" else 0.9,
        )
        now = datetime(2026, 11, 12, 12, tzinfo=UTC) + timedelta(
            seconds=rng.randint(-30 * 24 * 60 * 60, 30 * 24 * 60 * 60)
        )
        length = DURATION_SPANS[text]
        window_start = now - length * rng.uniform(-0.25, 1.5)
        resets_at = window_start + length
        observed_at = now + timedelta(seconds=rng.randint(-3 * 24 * 60 * 60, 3 * 24 * 60 * 60))
        # ledger usage is capped at 1.0 (ledger: min(..., 1.0)); edges set explicitly
        used = rng.uniform(0.0, 1.0)
        if case % 11 == 0:
            used = 1.0
        elif case % 13 == 0:
            used = 0.0
        known = case % 7 != 0
        state = WindowState(
            pool="pool",
            window=window.id,
            known=known,
            used=used if known else None,
            source="manual" if known else None,
            observed_at=observed_at if known else None,
            window_start=window_start,
            resets_at=resets_at,
            estimated=False,
            note=None if known else "no data",
        )
        case_id = (
            f"seed={PACE_SEED};case={case};duration={text};kind={kind};role={role};"
            f"used={used:.17g};known={known};now={now.isoformat()};"
            f"observed_at={observed_at.isoformat()};window_start={window_start.isoformat()};"
            f"resets_at={resets_at.isoformat()}"
        )
        cases.append(pytest.param(window, state, now, id=case_id))
    return cases


@pytest.mark.parametrize(("window", "state", "now"), _pace_cases())
def test_window_pace_lag_is_bounded(window: Window, state: WindowState, now: datetime):
    pace = window_pace(window, state, now)
    lag = pace.lag

    if lag is not None:
        assert pace.expected is not None and 0.0 <= pace.expected <= 1.0
        assert math.isfinite(lag)
        assert -1.0 <= lag <= 1.0
