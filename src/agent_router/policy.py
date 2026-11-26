"""Route choice function: pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from agent_router.config import TIERS, Mode, Route, RouteKind, Tier
from agent_router.pacing import PoolStatus

NO_DATA = "no data, calibrate"
CALLER_POOL_REASON = "caller pool"


@dataclass(frozen=True)
class RouteRequest:
    tier: Tier
    mode: Mode | None = None
    caller_pool: str | None = None
    excluded_routes: frozenset[str] = frozenset()
    excluded_pools: frozenset[str] = frozenset()
    request_excluded_pools: frozenset[str] = frozenset()
    explicit: bool = False  # explicit route: reserve and caller pool do not apply
    kind: RouteKind = "harness"
    model_group: str | None = None


@dataclass(frozen=True)
class RouteView:
    route: Route
    available: bool = True
    unavailable_reason: str | None = None
    priority: int = 0
    weight: float = 0.0
    prefer_rank: int | None = None


@dataclass(frozen=True)
class PoolBlock:
    pool: str
    until: datetime
    reason: str


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    tz: tzinfo | None  # None: the system zone at the moment of formatting
    pools: Mapping[str, PoolStatus]
    blocks: Mapping[str, PoolBlock]


@dataclass(frozen=True)
class Choice:
    route: Route
    reason: str
    note: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Exclusion:
    route: Route
    reason: str


@dataclass(frozen=True)
class RouteMetrics:
    """Route metrics that build the `decide` sort key."""

    pool: str
    priority: int
    weight: float
    lag: float | None
    score: float
    quality: int
    has_data: bool
    prefer_rank: int | None
    urgency: float | None


@dataclass(frozen=True)
class Decision:
    chosen: tuple[Choice, ...]
    excluded: tuple[Exclusion, ...]
    metrics: Mapping[str, RouteMetrics] = field(default_factory=dict, compare=False)


def _clock(moment: datetime, snapshot: Snapshot) -> str:
    local = moment.astimezone(snapshot.tz)
    fmt = "%H:%M" if local.date() == snapshot.now.astimezone(snapshot.tz).date() else "%d.%m %H:%M"
    return local.strftime(fmt)


def _percent(share: float) -> int:
    return round(share * 100)


def _exclusion(request: RouteRequest, snapshot: Snapshot, view: RouteView) -> str | None:
    """Exclusion reason for a route; checks run in a fixed order, the first match wins."""
    route = view.route
    if not route.enabled:
        return "route disabled"
    if not view.available:
        return f"harness unavailable: {view.unavailable_reason or 'reason unknown'}"
    if not request.explicit and request.tier not in route.tiers:
        return f"does not serve tier {request.tier}"
    if request.mode is not None and request.mode not in route.modes:
        return f"mode {request.mode} not supported"
    if route.pool in request.request_excluded_pools:
        return "excluded by request"
    if route.id in request.excluded_routes or route.pool in request.excluded_pools:
        return "excluded for this job"
    if not request.explicit and request.caller_pool == route.pool:
        return CALLER_POOL_REASON
    block = snapshot.blocks.get(route.pool)
    if block is not None and block.until > snapshot.now:
        return f"pool closed until {_clock(block.until, snapshot)}: {block.reason}"
    status = snapshot.pools.get(route.pool)
    if status is None:
        return None
    if status.blocked_by is not None:
        if status.blocked_until is None:
            return f"window {status.blocked_by} exhausted"
        until = _clock(status.blocked_until, snapshot)
        return f"window {status.blocked_by} exhausted until {until}"
    reserve_tier = status.pool.reserve_tier
    if (
        not request.explicit
        and status.reserve_hit
        and reserve_tier is not None
        and TIERS.index(request.tier) < TIERS.index(reserve_tier)
    ):
        return f"reserve {_percent(status.pool.reserve)}% for {reserve_tier} tier jobs"
    return None


def _pace_reason(status: PoolStatus) -> str:
    """Choice reason from the window that determines the pool's urgency (the smallest urgency)."""
    paces = [pace for pace in status.windows if pace.urgency is not None]
    pace = min(paces, key=lambda item: (item.urgency, item.window.id))
    lag = pace.lag or 0.0
    expected = pace.expected or 0.0
    verb = "behind pace" if lag >= 0 else "ahead of pace"
    detail = (
        f"{pace.window.id}: {_percent(expected)}% of time elapsed,"
        f" {_percent(pace.state.used or 0.0)}% used,"
        f" {_percent(1.0 - expected)}% left"
    )
    return f"{verb} by {_percent(abs(lag))} pp ({detail})"


def _choice_note(status: PoolStatus | None, has_data: bool) -> str | None:
    if status is None:
        return None
    if has_data:
        paces = [pace for pace in status.windows if pace.urgency is not None]
        return min(paces, key=lambda item: (item.urgency, item.window.id)).state.note
    pace_note = next(
        (
            pace.state.note
            for pace in status.windows
            if pace.window.role == "pace" and pace.state.note
        ),
        None,
    )
    return pace_note or next((pace.state.note for pace in status.windows if pace.state.note), None)


def _ordinal(rank: int) -> str:
    """Preference rank as a word or ordinal: 'first' for 0, then 2nd, 3rd, ..."""
    if rank == 0:
        return "first"
    place = rank + 1
    teens = 10 <= place % 100 <= 20
    suffix = "th" if teens else {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
    return f"{place}{suffix}"


def decide(request: RouteRequest, snapshot: Snapshot, routes: Sequence[RouteView]) -> Decision:
    """Eligible routes in preference order and excluded ones; each carries its reason string."""
    groups: dict[str, list[tuple[tuple[int, int, float, int, str], RouteView, Choice]]] = {}
    metrics: dict[str, RouteMetrics] = {}
    excluded: list[Exclusion] = []
    for view in routes:
        route = view.route
        if route.kind != request.kind:
            continue
        if request.model_group is not None and request.model_group != route.model_group:
            continue
        excluded_reason = _exclusion(request, snapshot, view)
        if excluded_reason is not None:
            excluded.append(Exclusion(route, excluded_reason))
            continue
        priority = 0 if request.explicit else view.priority
        weight = 0.0 if request.explicit else view.weight
        status = snapshot.pools.get(route.pool)
        has_data = (
            status is not None
            and status.known
            and status.lag is not None
            and status.urgency is not None
        )
        if has_data:
            score = status.urgency * 100 + weight
            reason = _pace_reason(status)
            if weight != 0:
                reason += f", weight {weight:+g}".replace("-", "−")
        else:
            score = 0.0
            reason = NO_DATA
        if priority != 0:
            reason += f", priority {priority}"
        if view.prefer_rank is not None:
            rank = _ordinal(view.prefer_rank)
            reason += f"; model {route.model_group}: pool {route.pool} {rank} by preference"
        key = (priority, 0 if has_data else 1, -score, -route.quality, route.id)
        metrics[route.id] = RouteMetrics(
            pool=route.pool,
            priority=priority,
            weight=weight,
            lag=None if status is None else status.lag,
            score=score,
            quality=route.quality,
            has_data=has_data,
            prefer_rank=view.prefer_rank,
            urgency=None if status is None else status.urgency,
        )
        choice = Choice(route, reason, _choice_note(status, has_data))
        groups.setdefault(route.model_group, []).append((key, view, choice))

    ordered_groups = []
    for model_group, members in groups.items():
        members.sort(
            key=lambda item: (
                (0, item[1].prefer_rank, item[0])
                if item[1].prefer_rank is not None
                else (1, 0, item[0])
            )
        )
        ordered_groups.append((members[0][0], model_group, members))
    ordered_groups.sort(key=lambda item: (item[0], item[1]))
    excluded.sort(key=lambda item: item.route.id)
    return Decision(
        chosen=tuple(choice for _, _, members in ordered_groups for _, _, choice in members),
        excluded=tuple(excluded),
        metrics=metrics,
    )
