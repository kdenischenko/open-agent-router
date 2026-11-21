from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_router import ledger as ledger_module
from agent_router.config import Pool, Window
from agent_router.ledger import window_state
from agent_router.periods import parse_duration
from agent_router.store import CounterSample, Observation

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TOL = timedelta(minutes=15)


def win(id="week", duration="7d", kind="fixed", role="pace", anchor=None, limit_usd=None):
    block_at = 1.0 if role == "pace" else 0.9
    return Window(id, parse_duration(duration), kind, role, anchor, limit_usd, block_at)


def make_pool(*windows, unit="percent", counter=None):
    return Pool("p", "Pool", unit, tuple(windows), 0.0, None, counter, None, None)


def usd_pool(window):
    return make_pool(window, unit="usd", counter="opencode_stats")


def observe(store, window, fraction, at, resets=None, source="manual"):
    store.add_observation(Observation("p", window.id, fraction, at, resets, source))


def sample(store, value, at):
    store.add_counter_sample(CounterSample("p", value, at))


def hours(n):
    return timedelta(hours=n)


# --- fixed --------------------------------------------------------------------


def test_fixed_reset_ahead_uses_observation(store):
    w = win()
    reset = NOW + timedelta(days=2)
    observe(store, w, 0.38, NOW - hours(3), resets=reset, source="claude_statusline")
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is True
    assert state.used == 0.38
    assert state.source == "claude_statusline"
    assert state.observed_at == NOW - hours(3)
    assert state.resets_at == reset
    assert state.window_start == reset - timedelta(days=7)
    assert state.estimated is False and state.note is None
    assert (state.pool, state.window) == ("p", "week")


def test_fixed_reset_passed_gives_zero_and_next_reset(store):
    w = win()
    reset = NOW - timedelta(days=10)
    observe(store, w, 0.9, NOW - timedelta(days=12), resets=reset)
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is True
    assert state.used == 0
    assert state.source == "scheduled_reset"
    assert state.resets_at == reset + timedelta(days=14)
    assert state.resets_at > NOW
    assert state.window_start == reset + timedelta(days=7)
    assert state.observed_at == state.window_start


def test_moment_of_reset_belongs_to_new_window(store):
    w = win()
    observe(store, w, 0.9, NOW - hours(1), resets=NOW)
    state = window_state(make_pool(w), w, store, NOW)
    assert state.used == 0 and state.source == "scheduled_reset"
    assert state.window_start == NOW
    assert state.resets_at == NOW + timedelta(days=7)


def test_fixed_without_reset_and_anchor(store):
    w = win()
    observe(store, w, 0.5, NOW - hours(1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is True
    assert state.used == 0.5
    assert state.resets_at is None and state.window_start is None
    assert "reset time" in state.note


def test_fixed_without_reset_keeps_the_reset_of_an_earlier_observation(store):
    w = win()
    reset = NOW + timedelta(days=3)
    observe(store, w, 0.54, NOW - timedelta(days=4), resets=reset)
    observe(store, w, 0.57, NOW - timedelta(days=2))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is True
    assert state.used == 0.57
    assert state.resets_at == reset
    assert state.window_start == reset - timedelta(days=7)
    assert state.note is None


def test_earlier_reset_passed_before_the_observation_moves_to_the_next_window(store):
    w = win()
    observe(store, w, 0.9, NOW - timedelta(days=10), resets=NOW - timedelta(days=8))
    observe(store, w, 0.5, NOW - hours(1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.used == 0.5
    assert state.window_start == NOW - timedelta(days=1)
    assert state.resets_at == NOW + timedelta(days=6)


def test_earlier_reset_named_before_its_observation_is_not_kept(store):
    w = win()
    observe(store, w, 0.4, NOW - timedelta(days=2), resets=NOW - timedelta(days=3))
    observe(store, w, 0.5, NOW - hours(1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.resets_at is None
    assert "reset time" in state.note


def test_fixed_anchor_only_gives_bounds(store):
    anchor = datetime(2026, 11, 9, 9, tzinfo=UTC)
    w = win(anchor=anchor)
    observe(store, w, 0.5, NOW - hours(1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.used == 0.5
    assert state.window_start == anchor
    assert state.resets_at == anchor + timedelta(days=7)
    assert state.note is None


def test_fixed_anchor_only_reset_passed(store):
    anchor = datetime(2026, 11, 9, 9, tzinfo=UTC)
    w = win(anchor=anchor)
    observe(store, w, 0.5, anchor - hours(1))  # observation from the previous window
    state = window_state(make_pool(w), w, store, NOW)
    assert state.used == 0 and state.source == "scheduled_reset"
    assert (state.window_start, state.resets_at) == (anchor, anchor + timedelta(days=7))


def test_fixed_month_window_from_anchor(store):
    anchor = datetime(2026, 1, 31, tzinfo=UTC)
    w = win("month", "1mo", anchor=anchor)
    observe(store, w, 0.2, NOW - hours(1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.window_start == datetime(2026, 10, 31, tzinfo=UTC)
    assert state.resets_at == datetime(2026, 11, 30, tzinfo=UTC)


def test_observation_reset_wins_over_anchor(store):
    w = win(anchor=datetime(2026, 11, 9, 9, tzinfo=UTC))
    reset = NOW + hours(5)
    observe(store, w, 0.5, NOW - hours(1), resets=reset)
    assert window_state(make_pool(w), w, store, NOW).resets_at == reset


def test_reset_named_before_observation_is_ignored(store):
    w = win()
    observe(store, w, 0.5, NOW - hours(1), resets=NOW - hours(2))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.used == 0.5 and state.resets_at is None
    assert "reset time" in state.note


def test_no_observations(store):
    w = win()
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is False
    assert state.used is None and state.source is None and state.observed_at is None
    assert state.note == "no data"


def test_no_observations_with_anchor_still_shows_bounds(store):
    anchor = datetime(2026, 11, 9, 9, tzinfo=UTC)
    w = win(anchor=anchor)
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is False
    assert (state.window_start, state.resets_at) == (anchor, anchor + timedelta(days=7))


def test_observation_from_future_has_no_negative_age(store):
    w = win()
    observe(store, w, 0.3, NOW + hours(2), resets=NOW + hours(30))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.observed_at == NOW
    assert state.used == 0.3


def test_naive_now_rejected(store):
    w = win()
    with pytest.raises(ValueError):
        window_state(make_pool(w), w, store, datetime(2026, 11, 12, 12))


# --- rolling ------------------------------------------------------------------


def test_rolling_fresh_observation(store):
    w = win("5h", "5h", "rolling", "guard")
    observe(store, w, 0.95, NOW - hours(4))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is True and state.used == 0.95
    assert state.observed_at == NOW - hours(4)
    assert state.resets_at is None


def test_rolling_older_than_window_is_unknown(store):
    w = win("5h", "5h", "rolling", "guard")
    observe(store, w, 0.95, NOW - hours(5) - timedelta(seconds=1))
    state = window_state(make_pool(w), w, store, NOW)
    assert state.known is False and state.used is None
    assert "no data" in state.note


# --- usd: estimate from the counter ---------------------------------------------


def test_usd_estimate_from_counter(store):
    w = win(limit_usd=30.0)
    base_at = NOW - hours(2)
    observe(store, w, 0.40, base_at, resets=NOW + timedelta(days=1))
    sample(store, 10.0, base_at - timedelta(minutes=3))
    sample(store, 13.0, NOW - timedelta(minutes=1))
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.50)
    assert state.estimated is True
    assert state.source == "manual" and state.note is None


def test_usd_base_sample_taken_after_base_when_none_before(store):
    w = win(limit_usd=30.0)
    base_at = NOW - hours(2)
    observe(store, w, 0.40, base_at, resets=NOW + timedelta(days=1))
    sample(store, 10.0, base_at + timedelta(minutes=5))
    sample(store, 11.5, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.45)


def test_usd_after_scheduled_reset_base_is_zero_at_reset(store):
    w = win(limit_usd=30.0)
    reset = NOW - hours(3)
    observe(store, w, 0.80, NOW - hours(20), resets=reset)
    sample(store, 100.0, NOW - hours(20))
    sample(store, 106.0, reset - timedelta(minutes=2))  # spend from the previous window
    sample(store, 109.0, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.source == "scheduled_reset"
    assert state.used == pytest.approx(0.10)  # only $3 spent after the reset
    assert state.estimated is True


def test_usd_two_windows_in_a_row_do_not_leak(store):
    w = win("5h", "5h", limit_usd=12.0)
    first_reset = NOW - hours(7)  # one full window has passed since, a second is running
    observe(store, w, 0.5, first_reset - hours(1), resets=first_reset)
    sample(store, 50.0, first_reset)
    sample(store, 56.0, first_reset + hours(5))  # $6 was spent in the previous window
    sample(store, 57.2, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.window_start == first_reset + hours(5)
    assert state.used == pytest.approx(0.10)


def test_usd_estimate_capped_at_one(store):
    w = win(limit_usd=30.0)
    observe(store, w, 0.90, NOW - hours(1), resets=NOW + hours(5))
    sample(store, 10.0, NOW - hours(1))
    sample(store, 40.0, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.used == 1.0 and state.estimated is True


def test_usd_counter_decreased_adds_nothing(store):
    w = win(limit_usd=30.0)
    observe(store, w, 0.40, NOW - hours(1), resets=NOW + hours(5))
    sample(store, 10.0, NOW - hours(1))
    sample(store, 4.0, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40) and state.estimated is True


def test_usd_no_base_sample_after_observation(store):
    w = win(limit_usd=30.0)
    observe(store, w, 0.40, NOW - hours(2), resets=NOW + hours(5))
    sample(store, 13.0, NOW)  # beyond the tolerance from the base sample
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.known is True and state.used == 0.40
    assert state.estimated is False
    assert "counter" in state.note


def test_usd_no_base_sample_after_scheduled_reset(store):
    w = win(limit_usd=30.0)
    reset = NOW - hours(3)
    observe(store, w, 0.80, NOW - hours(20), resets=reset)
    sample(store, 13.0, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    # the only sample after the window start is the current one: nothing counted yet
    assert state.known is True and state.used == 0.0
    assert state.resets_at == reset + timedelta(days=7)
    assert ledger_module.NOTE_BASE_AFTER_START in state.note


def test_usd_tolerance_is_a_parameter(store):
    w = win(limit_usd=30.0)
    observe(store, w, 0.40, NOW - hours(2), resets=NOW + hours(5))
    sample(store, 10.0, NOW - hours(2) - timedelta(minutes=40))
    sample(store, 13.0, NOW)
    assert window_state(usd_pool(w), w, store, NOW, TOL).estimated is False
    wide = window_state(usd_pool(w), w, store, NOW, timedelta(minutes=45))
    assert wide.estimated is True and wide.used == pytest.approx(0.50)


def test_usd_pool_without_counter_has_no_estimate(store):
    w = win(limit_usd=30.0)
    observe(store, w, 0.40, NOW - hours(1), resets=NOW + hours(5))
    sample(store, 10.0, NOW - hours(1))
    sample(store, 13.0, NOW)
    state = window_state(make_pool(w, unit="usd"), w, store, NOW, TOL)
    assert state.used == 0.40 and state.estimated is False and state.note is None


def test_percent_pool_ignores_counter(store):
    w = win()
    observe(store, w, 0.40, NOW - hours(1), resets=NOW + hours(5))
    sample(store, 10.0, NOW - hours(1))
    sample(store, 13.0, NOW)
    state = window_state(make_pool(w), w, store, NOW, TOL)
    assert state.used == 0.40 and state.estimated is False


def test_ledger_reset_in_sampling_gap_uses_first_sample_after_start(store):
    w = win(limit_usd=30.0)
    reset = NOW - hours(12)  # 00:00; the laptop slept from 23:00 to 08:00
    observe(store, w, 0.80, NOW - hours(30), resets=reset)
    sample(store, 100.0, reset - hours(1))
    sample(store, 104.0, reset + hours(8))
    sample(store, 107.0, NOW)
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.source == "scheduled_reset"
    assert state.known is True
    assert state.used == pytest.approx((107.0 - 104.0) / 30.0)
    assert state.estimated is True
    assert ledger_module.NOTE_BASE_AFTER_START == (
        "counter: baseline sample taken after the window start"
    )
    assert ledger_module.NOTE_BASE_AFTER_START in state.note


def test_ledger_reset_without_any_later_sample_is_no_data(store):
    w = win(limit_usd=30.0)
    reset = NOW - hours(3)
    observe(store, w, 0.80, NOW - hours(20), resets=reset)
    sample(store, 100.0, reset - hours(1))
    state = window_state(usd_pool(w), w, store, NOW, TOL)
    assert state.known is False and state.used is None
    assert state.note == ledger_module.NOTE_NO_DATA
