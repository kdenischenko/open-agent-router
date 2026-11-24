"""Ledger: a pool's window state as of now."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from agent_router.clock import to_utc
from agent_router.config import Pool, Window
from agent_router.periods import shift, window_bounds
from agent_router.spend import SpendReader
from agent_router.store import CounterSample, Store

SCHEDULED_RESET = "scheduled_reset"
NOTE_NO_DATA = "no data"
NOTE_STALE = "no data: observation older than window"
NOTE_STALE_FIXED = "no data: observation older than max age"
NOTE_NO_RESET = "no data, set the reset time"
NOTE_NO_BASE_SAMPLE = "counter: no baseline sample"
NOTE_BASE_AFTER_START = "counter: baseline sample taken after the window start"
NOTE_SCALE_CHANGED = "counter: model weights changed — recalibrate the pool"
_RESET_LOOKBACK = 50  # observations searched for an earlier reset time


@dataclass(frozen=True)
class WindowState:
    pool: str
    window: str
    known: bool
    used: float | None
    source: str | None  # "manual" | "claude_statusline" | "scheduled_reset"
    observed_at: datetime | None
    window_start: datetime | None
    resets_at: datetime | None
    estimated: bool
    note: str | None
    estimated_prices: bool = False


def _unknown(pool: Pool, window: Window, note: str, **fields: datetime | None) -> WindowState:
    return WindowState(
        pool=pool.id,
        window=window.id,
        known=False,
        used=None,
        source=None,
        observed_at=fields.get("observed_at"),
        window_start=fields.get("window_start"),
        resets_at=fields.get("resets_at"),
        estimated=False,
        note=note,
    )


def _append_note(current: str | None, note: str) -> str:
    return note if current is None else f"{current}; {note}"


def _counter_spend(store: Store, pool_id: str, base_sample: CounterSample) -> tuple[float, bool]:
    """Spend since `base_sample`: growth between adjacent samples with equal model weights."""
    samples = store.counter_samples_since(pool_id, base_sample.sampled_at)
    spent = 0.0
    scale_changed = False
    for previous, following in zip(samples, samples[1:], strict=False):
        if previous.scale_hash == following.scale_hash:
            spent += max(0.0, following.value_usd - previous.value_usd)
        else:
            scale_changed = True
    return spent, scale_changed


def _earlier_reset(store: Store, pool: Pool, window: Window) -> datetime | None:
    """Latest trustworthy reset reported for the window, or None."""
    for obs in store.observations(pool.id, window.id, limit=_RESET_LOOKBACK):
        if obs.resets_at is not None and obs.resets_at > obs.observed_at:
            return obs.resets_at
    return None


def window_state(
    pool: Pool,
    window: Window,
    store: Store,
    now: datetime,
    base_tolerance: timedelta = timedelta(minutes=15),
    spend: SpendReader | None = None,
) -> WindowState:
    """Window state: latest observation, scheduled reset, counter-based estimate."""
    now = to_utc(now)
    obs = store.last_observation(pool.id, window.id)
    if obs is None:
        if window.kind == "fixed" and window.anchor is not None:
            start, end = window_bounds(window.anchor, window.duration, now)
            return _unknown(pool, window, NOTE_NO_DATA, window_start=start, resets_at=end)
        return _unknown(pool, window, NOTE_NO_DATA)

    observed_at = min(obs.observed_at, now)  # an observation "from the future" counts as made now
    state = WindowState(
        pool=pool.id,
        window=window.id,
        known=True,
        used=min(obs.fraction, 1.0),
        source=obs.source,
        observed_at=observed_at,
        window_start=None,
        resets_at=None,
        estimated=False,
        note=None,
    )
    base_moment = observed_at

    if window.kind == "rolling":
        if shift(observed_at, window.duration, 1) <= now:
            return _unknown(pool, window, NOTE_STALE, observed_at=observed_at)
    else:
        reset = obs.resets_at
        if reset is not None and reset <= observed_at:
            reset = None  # the reset reported by the observation lies in its past: untrustworthy
        origin = reset  # the moment from which subsequent resets are counted
        start = None if reset is None else shift(reset, window.duration, -1)
        if reset is None:
            # a calibration without a reset time keeps the schedule an earlier one reported
            origin = _earlier_reset(store, pool, window)
            if origin is not None:
                start, reset = window_bounds(origin, window.duration, observed_at)
        if reset is None and window.anchor is not None:
            origin = window.anchor  # a calendar month is counted from the original anchor
            start, reset = window_bounds(origin, window.duration, observed_at)
        if (
            (reset is None or reset > now)
            and window.max_age is not None
            and observed_at + window.max_age < now
        ):
            return _unknown(
                pool,
                window,
                NOTE_STALE_FIXED,
                observed_at=observed_at,
                window_start=start,
                resets_at=reset,
            )
        if reset is None or origin is None:
            state = replace(state, note=NOTE_NO_RESET)
        elif reset <= now:  # the moment equal to the reset belongs to the new window
            start, end = window_bounds(origin, window.duration, now)
            state = replace(
                state,
                used=0.0,
                source=SCHEDULED_RESET,
                observed_at=start,
                window_start=start,
                resets_at=end,
            )
            base_moment = start
        else:
            state = replace(state, window_start=start, resets_at=reset)

    if pool.unit != "usd" or not window.limit_usd:
        return state
    total = None if spend is None else spend.spend_since(pool.id, base_moment)
    spend_used = 0.0 if total is None else total.cost_usd / window.limit_usd
    estimated_prices = total is not None and total.estimated
    if pool.counter is None:
        if total is None:
            return state
        return replace(
            state,
            used=min(1.0, (state.used or 0.0) + spend_used),
            estimated_prices=estimated_prices,
        )
    base_sample = store.counter_at(pool.id, base_moment, base_tolerance)
    current = store.last_counter(pool.id)
    note = state.note
    if base_sample is None and current is not None and state.source == SCHEDULED_RESET:
        later = store.counter_samples_since(pool.id, base_moment)
        if later:
            base_sample = later[0]
            note = _append_note(note, NOTE_BASE_AFTER_START)
    if base_sample is None or current is None:
        if state.source == SCHEDULED_RESET:
            return replace(state, known=False, used=None, note=NOTE_NO_DATA)
        note = NOTE_NO_BASE_SAMPLE
        if spend is not None:
            note = _append_note(state.note, note)
        return replace(
            state,
            used=min(1.0, (state.used or 0.0) + spend_used),
            note=note,
            estimated_prices=estimated_prices,
        )
    spent, scale_changed = _counter_spend(store, pool.id, base_sample)
    if scale_changed:
        note = _append_note(note, NOTE_SCALE_CHANGED)
    used = min(1.0, (state.used or 0.0) + spent / window.limit_usd + spend_used)
    return replace(state, used=used, estimated=True, estimated_prices=estimated_prices, note=note)
