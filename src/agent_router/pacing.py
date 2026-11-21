"""Usage pacing, pool blocking, and reserve handling."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_router.clock import to_utc
from agent_router.config import Pool, Window
from agent_router.ledger import WindowState
from agent_router.periods import shift

_EPS = 1e-9  # 1 - 0.85 in floating point is slightly greater than 0.15


@dataclass(frozen=True)
class WindowPace:
    window: Window
    state: WindowState
    expected: float | None
    lag: float | None
    blocked: bool


@dataclass(frozen=True)
class PoolStatus:
    pool: Pool
    windows: tuple[WindowPace, ...]
    known: bool
    lag: float | None
    blocked_by: str | None
    blocked_until: datetime | None
    reserve_hit: bool


def window_pace(window: Window, state: WindowState, now: datetime) -> WindowPace:
    """Expected usage and lag, computed only for a pace window with known usage and reset."""
    now = to_utc(now)
    used = state.used if state.known else None
    expected = lag = None
    if (
        window.role == "pace"
        and used is not None
        and state.window_start is not None
        and state.resets_at is not None
        and state.resets_at > state.window_start
    ):
        share = (now - state.window_start) / (state.resets_at - state.window_start)
        expected = min(1.0, max(0.0, share))
        lag = expected - used
    blocked = used is not None and used >= window.block_at - _EPS
    return WindowPace(window=window, state=state, expected=expected, lag=lag, blocked=blocked)


def blocked_until(pace: WindowPace) -> datetime | None:
    """When the window stops blocking the pool; None if the deadline is unknown."""
    if not pace.blocked:
        return None
    if pace.window.kind == "rolling":
        if pace.state.observed_at is None:
            return None
        return shift(pace.state.observed_at, pace.window.duration, 1)
    return pace.state.resets_at


def _unknown_state(pool: Pool, window: Window) -> WindowState:
    return WindowState(
        pool=pool.id,
        window=window.id,
        known=False,
        used=None,
        source=None,
        observed_at=None,
        window_start=None,
        resets_at=None,
        estimated=False,
        note="no data",
    )


def pool_status(pool: Pool, states: Sequence[WindowState], now: datetime) -> PoolStatus:
    """Pool summary: lag is the minimum across pace windows; the latest-deadline window blocks."""
    by_window = {state.window: state for state in states}
    paces = tuple(
        window_pace(window, by_window.get(window.id) or _unknown_state(pool, window), now)
        for window in pool.windows
    )
    lags = [pace.lag for pace in paces if pace.lag is not None]

    blocked_by: str | None = None
    until: datetime | None = None
    for pace in paces:
        if not pace.blocked:
            continue
        moment = blocked_until(pace)
        if blocked_by is None:
            blocked_by, until = pace.window.id, moment
        elif until is not None and (moment is None or moment > until):
            blocked_by, until = pace.window.id, moment  # unknown deadline outlasts any known

    reserve_hit = pool.reserve > 0 and any(
        pace.window.role == "pace"
        and pace.state.known
        and pace.state.used is not None
        and 1.0 - pace.state.used <= pool.reserve + _EPS
        for pace in paces
    )
    return PoolStatus(
        pool=pool,
        windows=paces,
        known=bool(lags),
        lag=min(lags) if lags else None,
        blocked_by=blocked_by,
        blocked_until=until,
        reserve_hit=reserve_hit,
    )
