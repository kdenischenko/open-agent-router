"""Route scorecard: one row per route x tier x mode from job history and ratings."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent_router.config import Mode, Tier

_OUTCOME_STATUSES = ("completed", "partial", "failed", "cancelled")
_ATTEMPT_OUTCOMES = ("limit", "error", "timeout")
_RATING_VALUES = ("good", "partial", "bad")


@dataclass(frozen=True)
class JobSample:
    """One finished job: its final route, outcome, latest rating and attempt totals."""

    route: str | None
    tier: Tier
    mode: Mode
    status: str
    rating: str | None
    duration_s: float | None
    tokens: int | None
    cost_usd: float | None
    cost_estimated: bool


@dataclass(frozen=True)
class AttemptSample:
    """One finished attempt: its own route, the tier and mode of its job."""

    route: str
    tier: Tier
    mode: Mode
    outcome: str


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return ordered[rank - 1]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build(
    jobs: Iterable[JobSample],
    attempts: Iterable[AttemptSample],
    quality: dict[str, int],
    min_jobs: int = 5,
) -> dict[str, Any]:
    """Scorecard rows: counters, shares and medians; ranked rows come first.

    A job without a final route is skipped. Rows with fewer than `min_jobs` jobs are
    listed after the ranked ones in the same order.
    """
    grouped: dict[tuple[str, Tier, Mode], list[JobSample]] = {}
    for sample in jobs:
        if sample.route is None:
            continue
        grouped.setdefault((sample.route, sample.tier, sample.mode), []).append(sample)
    counted: dict[tuple[str, Tier, Mode], dict[str, int]] = {}
    for sample in attempts:
        if sample.outcome not in _ATTEMPT_OUTCOMES:
            continue
        counts = counted.setdefault(
            (sample.route, sample.tier, sample.mode), dict.fromkeys(_ATTEMPT_OUTCOMES, 0)
        )
        counts[sample.outcome] += 1
    rows: list[dict[str, Any]] = []
    for key in sorted(grouped.keys() | counted.keys()):
        route, tier, mode = key
        samples = grouped.get(key, [])
        outcomes = dict.fromkeys(_OUTCOME_STATUSES, 0)
        ratings = dict.fromkeys(_RATING_VALUES, 0)
        durations: list[float] = []
        tokens: list[float] = []
        costs: list[float] = []
        estimated = False
        for sample in samples:
            if sample.status in outcomes:
                outcomes[sample.status] += 1
            if sample.rating in ratings:
                ratings[sample.rating] += 1  # type: ignore[literal-required]
            if sample.duration_s is not None:
                durations.append(sample.duration_s)
            if sample.tokens is not None:
                tokens.append(sample.tokens)
            if sample.cost_usd is not None:
                costs.append(sample.cost_usd)
            estimated = estimated or sample.cost_estimated
        total = len(samples)
        rated = sum(ratings.values())
        rows.append(
            {
                "route": route,
                "tier": tier,
                "mode": mode,
                "in_config": route in quality,
                "quality": quality.get(route),
                "jobs": total,
                "outcomes": outcomes,
                "attempts": counted.get(key, dict.fromkeys(_ATTEMPT_OUTCOMES, 0)),
                "ratings": ratings,
                "rated": rated,
                "rated_share": (rated / total) if total else 0.0,
                "good_share": (ratings["good"] / rated) if rated else None,
                "duration_s": {
                    "median": _median(durations),
                    "p90": _p90(durations) if durations else None,
                },
                "tokens_median": _median(tokens),
                "cost_median": _median(costs),
                "cost_estimated": estimated,
                "ranked": total >= min_jobs,
            }
        )

    def order(row: dict[str, Any]) -> tuple:
        share = row["good_share"]
        return (
            not row["ranked"],
            share is None,
            -(share if share is not None else 0.0),
            -row["jobs"],
            row["route"],
            row["tier"],
            row["mode"],
        )

    rows.sort(key=order)
    return {"min_jobs": min_jobs, "rows": rows}
