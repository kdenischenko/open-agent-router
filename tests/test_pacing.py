from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_router.config import Pool, Window
from agent_router.ledger import WindowState
from agent_router.pacing import pool_status, window_pace
from agent_router.periods import parse_duration

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


def win(id="week", duration="7d", kind="fixed", role="pace", block_at=None):
    if block_at is None:
        block_at = 1.0 if role == "pace" else 0.9
    return Window(id, parse_duration(duration), kind, role, None, None, block_at)


def make_pool(*windows, reserve=0.0, reserve_tier=None):
    return Pool("p", "Pool", "percent", tuple(windows), reserve, reserve_tier, None, None, None)


def known(window, used, start=None, resets=None, observed=None):
    return WindowState(
        "p", window.id, True, used, "manual", observed or NOW, start, resets, False, None
    )


def unknown(window):
    return WindowState("p", window.id, False, None, None, None, None, None, False, "no data")


def fixed_state(window, used, elapsed, length=timedelta(days=7)):
    """State of a fixed window after a fraction `elapsed` has passed."""
    start = NOW - length * elapsed
    return known(window, used, start, start + length)


def test_lag_is_expected_minus_used():
    w = win()
    pace = window_pace(w, fixed_state(w, 0.38, 0.61), NOW)
    assert pace.expected == pytest.approx(0.61)
    assert pace.lag == pytest.approx(0.23)
    assert pace.blocked is False


def test_negative_lag_when_ahead_of_schedule():
    w = win()
    assert window_pace(w, fixed_state(w, 0.80, 0.50), NOW).lag == pytest.approx(-0.30)


def test_no_lag_without_reset_time():
    w = win()
    pace = window_pace(w, known(w, 0.5), NOW)
    assert pace.expected is None and pace.lag is None
    assert pace.blocked is False


def test_guard_window_has_no_lag():
    w = win("5h", "5h", "fixed", "guard")
    pace = window_pace(w, fixed_state(w, 0.5, 0.5, timedelta(hours=5)), NOW)
    assert pace.expected is None and pace.lag is None


def test_rolling_guard_blocks_until_observation_plus_length():
    guard = win("5h", "5h", "rolling", "guard")
    week = win()
    observed = NOW - timedelta(hours=1)
    status = pool_status(
        make_pool(guard, week),
        [known(guard, 0.95, observed=observed), fixed_state(week, 0.2, 0.5)],
        NOW,
    )
    assert status.windows[0].blocked is True
    assert status.blocked_by == "5h"
    assert status.blocked_until == observed + timedelta(hours=5)
    assert status.lag == pytest.approx(0.3)  # blocking does not cancel the pace computation


def test_fixed_guard_at_threshold_blocks_until_its_reset():
    guard = win("5h", "5h", "fixed", "guard")
    state = fixed_state(guard, 0.90, 0.4, timedelta(hours=5))
    status = pool_status(make_pool(guard, win()), [state], NOW)
    assert status.blocked_by == "5h"
    assert status.blocked_until == state.resets_at


def test_guard_below_threshold_does_not_block():
    guard = win("5h", "5h", "fixed", "guard")
    state = fixed_state(guard, 0.89, 0.4, timedelta(hours=5))
    status = pool_status(make_pool(guard, win()), [state], NOW)
    assert status.windows[0].blocked is False
    assert status.blocked_by is None and status.blocked_until is None


def test_pace_window_blocks_at_full():
    w = win()
    state = fixed_state(w, 1.0, 0.5)
    status = pool_status(make_pool(w), [state], NOW)
    assert status.blocked_by == "week"
    assert status.blocked_until == state.resets_at
    assert pool_status(make_pool(w), [fixed_state(w, 0.99, 0.5)], NOW).blocked_by is None


def test_custom_block_at():
    w = win(block_at=0.5)
    assert window_pace(w, fixed_state(w, 0.5, 0.5), NOW).blocked is True
    assert window_pace(w, fixed_state(w, 0.49, 0.5), NOW).blocked is False


def test_latest_deadline_wins_between_blocking_windows():
    guard = win("5h", "5h", "rolling", "guard")
    week = win()
    week_state = fixed_state(week, 1.0, 0.5)
    status = pool_status(make_pool(guard, week), [known(guard, 0.95), week_state], NOW)
    assert status.blocked_by == "week"
    assert status.blocked_until == week_state.resets_at


def test_blocking_window_without_deadline_wins():
    guard = win("5h", "5h", "rolling", "guard")
    week = win()
    status = pool_status(make_pool(guard, week), [known(guard, 0.95), known(week, 1.0)], NOW)
    assert status.blocked_by == "week"
    assert status.blocked_until is None


@pytest.mark.parametrize(("used", "hit"), [(0.86, True), (0.85, True), (0.84, False)])
def test_reserve(used, hit):
    w = win()
    pool = make_pool(w, reserve=0.15, reserve_tier="strong")
    assert pool_status(pool, [fixed_state(w, used, 0.9)], NOW).reserve_hit is hit


def test_reserve_ignores_guard_windows_and_zero_reserve():
    guard = win("5h", "5h", "rolling", "guard", block_at=0.99)
    week = win()
    states = [known(guard, 0.95), fixed_state(week, 0.2, 0.5)]
    pool = make_pool(guard, week, reserve=0.15, reserve_tier="strong")
    assert pool_status(pool, states, NOW).reserve_hit is False
    assert pool_status(make_pool(week), [fixed_state(week, 0.99, 0.5)], NOW).reserve_hit is False


def test_pool_lag_is_minimum_of_pace_windows():
    week = win()
    month = win("month", "1mo")
    states = [
        fixed_state(week, 0.38, 0.61),
        fixed_state(month, 0.50, 0.40, timedelta(days=30)),
    ]
    status = pool_status(make_pool(week, month), states, NOW)
    assert status.known is True
    assert status.lag == pytest.approx(-0.10)
    assert [pace.window.id for pace in status.windows] == ["week", "month"]


def test_all_windows_unknown():
    guard = win("5h", "5h", "rolling", "guard")
    week = win()
    pool = make_pool(guard, week, reserve=0.15, reserve_tier="strong")
    status = pool_status(pool, [unknown(guard), unknown(week)], NOW)
    assert status.known is False and status.lag is None
    assert status.blocked_by is None and status.reserve_hit is False


def test_unknown_window_neither_blocks_nor_hits_reserve():
    week = win()
    stale = WindowState("p", "week", False, 1.0, "manual", NOW, None, None, False, "no data")
    pool = make_pool(week, reserve=0.15, reserve_tier="strong")
    status = pool_status(pool, [stale], NOW)
    assert status.blocked_by is None and status.reserve_hit is False


def test_missing_state_counts_as_unknown():
    guard = win("5h", "5h", "rolling", "guard")
    week = win()
    status = pool_status(make_pool(guard, week), [fixed_state(week, 0.2, 0.5)], NOW)
    assert len(status.windows) == 2
    assert status.windows[0].state.known is False
    assert status.lag == pytest.approx(0.3)
