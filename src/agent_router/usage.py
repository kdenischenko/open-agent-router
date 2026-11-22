"""Usage sources: the seam between vendor usage endpoints and window observations.

A usage source reports the used share of a window together with a reset time, unlike a
counter which reports cumulative spend. This module holds the data types and the mapping
onto observations only; sources themselves are discovered via entry points and contain
their own transport code.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from agent_router.clock import to_utc
from agent_router.config_model import Pool, PoolUsage, Window
from agent_router.periods import shift
from agent_router.store import Observation

if TYPE_CHECKING:
    from agent_router.usage_reader import UsageReadState


@dataclass(frozen=True)
class UsagePoint:
    """One reported field: the used share of a window and when it resets."""

    field: str  # source field name, e.g. "monthly"
    fraction: float  # used share 0..1
    resets_at: datetime | None = None  # UTC


@dataclass(frozen=True)
class UsageRead:
    """The result of one source read: data points or a reason code when there is none."""

    points: tuple[UsagePoint, ...] = ()
    reason: str | None = None  # e.g. "unauthorized"

    @property
    def ok(self) -> bool:
        return self.reason is None


class UsageSource(Protocol):
    def read(self) -> UsageRead: ...


@dataclass(frozen=True)
class UsageSourceSpec:
    """Registration of a usage source: how to build it and what it can report."""

    name: str
    factory: Callable[[PoolUsage, Mapping[str, str]], UsageSource]  # pool usage, environment
    windows: frozenset[str]  # source field names the source can report
    observation_source: str  # value of Observation.source, e.g. "opencode_go_usage"
    # the variable doctor checks for presence when the pool sets no api_key_env
    default_api_key_env: str | None = None
    # doctor's own check of the source's prerequisites: a warning text, or None when all is well
    check: Callable[[PoolUsage, Mapping[str, str]], str | None] | None = None


def is_stale(state: UsageReadState | None, now: datetime, interval_s: float) -> bool:
    """True when no successful read is fresher than two read intervals.

    A pool without state or without a single successful read counts as stale. The
    caller checks the auto mode first: only auto mode promises reads at all, so
    web, status and doctor share this one threshold.
    """
    if state is None or state.last_success_at is None:
        return True
    return (to_utc(now) - to_utc(state.last_success_at)).total_seconds() > 2.0 * interval_s


def _valid_fraction(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and 0.0 <= float(value) <= 1.0


RESET_SLACK = timedelta(minutes=5)  # same tolerance as manual calibration


def _reset_for(window: Window | None, resets_at: datetime | None, now: datetime) -> datetime | None:
    """The reset time worth recording for a window, or None.

    A rolling window has no reset time of its own, and the ledger rejects one. For a fixed
    window, a reset that is not in the future or lies more than one window ahead is dropped
    the way manual calibration refuses it; the figure itself is still recorded.
    """
    if resets_at is None or window is None or window.kind == "rolling":
        return None
    if resets_at <= now or resets_at > shift(now, window.duration, 1) + RESET_SLACK:
        return None
    return resets_at


def observations(
    pool: Pool, spec: UsageSourceSpec, read: UsageRead, observed_at: datetime
) -> list[Observation]:
    """Map a read onto observations of the pool's windows.

    Only fields mapped in the pool's usage windows are used; points with a fraction
    outside 0..1 (or NaN) are dropped instead of raising.
    """
    if not read.ok:
        return []
    by_id = {window.id: window for window in pool.windows}
    result: list[Observation] = []
    for point in read.points:
        window = pool.usage.windows.get(point.field)
        if window is None:
            continue
        if not _valid_fraction(point.fraction):
            continue
        result.append(
            Observation(
                pool=pool.id,
                window=window,
                fraction=float(point.fraction),
                observed_at=observed_at,
                resets_at=_reset_for(by_id.get(window), point.resets_at, observed_at),
                source=spec.observation_source,
            )
        )
    return result
