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
from datetime import datetime
from typing import Protocol

from agent_router.config_model import Pool, PoolUsage
from agent_router.store import Observation


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


def _valid_fraction(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and 0.0 <= float(value) <= 1.0


def observations(
    pool: Pool, spec: UsageSourceSpec, read: UsageRead, observed_at: datetime
) -> list[Observation]:
    """Map a read onto observations of the pool's windows.

    Only fields mapped in the pool's usage windows are used; points with a fraction
    outside 0..1 (or NaN) are dropped instead of raising.
    """
    if not read.ok:
        return []
    kinds = {window.id: window.kind for window in pool.windows}
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
                # a rolling window has no reset time of its own; the ledger rejects one
                resets_at=None if kinds.get(window) == "rolling" else point.resets_at,
                source=spec.observation_source,
            )
        )
    return result
