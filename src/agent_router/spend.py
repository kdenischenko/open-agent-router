"""Gateway spend records and their roll-up per pool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from agent_router.config import Pool, Route, model_key
from agent_router.jobs import Attempt
from agent_router.prices import estimate


@dataclass(frozen=True)
class SpendEntry:
    pool: str
    at: datetime
    source: Literal["job", "proxy"]
    route: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    estimated: bool
    prices_version: str | None
    ref_id: str
    ref_n: int


@dataclass(frozen=True)
class SpendTotal:
    jobs: int
    requests: int
    tokens: int
    cost_usd: float
    estimated: bool


class SpendReader(Protocol):
    def spend_since(self, pool: str, since: datetime | None) -> SpendTotal: ...


def estimate_attempt_cost(attempt: Attempt, route: Route, pool: Pool) -> tuple[float, str] | None:
    """Estimate the cost of an attempt from the pool's price_file and the route's model weight.

    Returns None when the harness already reported the cost, or when prices, tokens, or a
    price for the model are missing. Calling again for an already estimated attempt
    recomputes the same value.
    """
    if attempt.cost_usd is not None and not attempt.cost_estimated:
        return None
    prices = pool.prices
    if prices is None:
        return None
    if attempt.tokens_in is None and attempt.tokens_out is None:
        return None
    key = model_key(route.model) if route.model else None
    cost = None if key is None else estimate(prices, key, attempt.tokens_in, attempt.tokens_out)
    if cost is None:  # fall back to the model group (older price_file)
        cost = estimate(prices, route.model_group, attempt.tokens_in, attempt.tokens_out)
    if cost is None:
        return None
    model = route.model if route.model is not None else route.model_group
    return cost * pool.scale(model), prices.version


def entry_for_attempt(attempt: Attempt, prices_version: str | None = None) -> SpendEntry:
    assert attempt.finished_at is not None
    return SpendEntry(
        pool=attempt.pool,
        at=attempt.finished_at,
        source="job",
        route=attempt.route,
        tokens_in=attempt.tokens_in,
        tokens_out=attempt.tokens_out,
        cost_usd=attempt.cost_usd,
        estimated=attempt.cost_estimated,
        prices_version=prices_version if attempt.cost_estimated else None,
        ref_id=attempt.job_id,
        ref_n=attempt.n,
    )
