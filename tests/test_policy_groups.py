"""Policy: model groups, prefer, priority, and weight."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_router.config import Pool, Route, Window
from agent_router.ledger import WindowState
from agent_router.pacing import pool_status
from agent_router.periods import parse_duration
from agent_router.policy import (
    NO_DATA,
    Choice,
    Decision,
    Exclusion,
    PoolBlock,
    RouteRequest,
    RouteView,
    Snapshot,
    _exclusion,
    _pace_reason,
    decide,
)
from agent_router.service import route_views
from helpers import full_config_text, write_config

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TZ = timezone(timedelta(hours=3))
WEEK = Window("week", parse_duration("7d"), "fixed", "pace", None, None, 1.0)


def pool_of(pool_id: str, reserve: float = 0.0, reserve_tier=None) -> Pool:
    return Pool(pool_id, pool_id, "percent", (WEEK,), reserve, reserve_tier, None, None, None)


def status(pool_id: str, lag: float | None, **pool_fields):
    """Pool summary with lag halfway through the week; lag=None means no data."""
    states = []
    if lag is not None:
        start = NOW - timedelta(days=3.5)
        states.append(
            WindowState(
                pool_id,
                "week",
                True,
                0.5 - lag,
                "manual",
                NOW,
                start,
                start + timedelta(days=7),
                False,
                None,
            )
        )
    return pool_status(pool_of(pool_id, **pool_fields), states, NOW)


def snapshot(*statuses, blocks=()) -> Snapshot:
    return Snapshot(
        now=NOW,
        tz=TZ,
        pools={item.pool.id: item for item in statuses},
        blocks={pool: PoolBlock(pool, NOW + timedelta(hours=2), "limit") for pool in blocks},
    )


def make_route(route_id: str, pool: str, quality: int = 3, **fields) -> Route:
    base = {
        "backend": "kimi",
        "model": None,
        "confirm": False,
        "enabled": True,
        "timeout_s": None,
    }
    base.update(fields)
    return Route(
        id=route_id,
        pool=pool,
        tiers=frozenset({"cheap", "standard", "strong"}),
        quality=quality,
        modes=frozenset({"read", "write"}),
        **base,
    )


def ids(decision: Decision) -> list[str]:
    return [choice.route.id for choice in decision.chosen]


def reason_of(decision: Decision, route_id: str) -> str:
    return next(choice.reason for choice in decision.chosen if choice.route.id == route_id)


CHEAP = RouteRequest("cheap")


def _decide_reference(request, snap, routes) -> Decision:
    """Reference copy of policy.decide without priority, weight, and prefer."""
    known = []
    unknown = []
    excluded = []
    for view in routes:
        reason = _exclusion(request, snap, view)
        if reason is not None:
            excluded.append(Exclusion(view.route, reason))
            continue
        item = snap.pools.get(view.route.pool)
        if item is None or not item.known or item.lag is None:
            unknown.append(Choice(view.route, NO_DATA))
        else:
            known.append((item.lag, Choice(view.route, _pace_reason(item))))
    known.sort(key=lambda pair: (-pair[0], -pair[1].route.quality, pair[1].route.id))
    unknown.sort(key=lambda choice: (-choice.route.quality, choice.route.id))
    excluded.sort(key=lambda item: item.route.id)
    return Decision(
        chosen=tuple(choice for _, choice in known) + tuple(unknown),
        excluded=tuple(excluded),
    )


REFERENCE_ROUTES = [
    make_route("a/1", "a", 3),
    make_route("a/2", "a", 5),
    make_route("b/1", "b", 4),
    make_route("c/1", "c", 2),
    make_route("d/1", "d", 4),
    make_route("off/1", "a", 5, enabled=False),
]

REFERENCE_SNAPSHOTS = {
    "with-data": snapshot(status("a", 0.1), status("b", 0.2), status("c", -0.1), status("d", 0.0)),
    "no-data": snapshot(status("a", None), status("b", None), status("c", None), status("d", None)),
    "mixed": snapshot(status("a", 0.1), status("b", None), status("c", -0.3)),
    "equal-lags": snapshot(status("a", 0.1), status("b", 0.1), status("c", 0.1), status("d", 0.1)),
    "blocked": snapshot(status("a", 0.1), status("b", 0.2), blocks=("b",)),
}


def test_neutral_levers_match_reference() -> None:
    view = RouteView(REFERENCE_ROUTES[0])
    assert view.priority == 0
    assert view.weight == 0.0
    assert view.prefer_rank is None
    views = [RouteView(route) for route in REFERENCE_ROUTES]
    for name, snap in REFERENCE_SNAPSHOTS.items():
        for request in (CHEAP, RouteRequest("standard", caller_pool="c")):
            assert decide(request, snap, views) == _decide_reference(request, snap, views), name


def test_weight_shifts_by_points() -> None:
    a = make_route("a/1", "a")
    b = make_route("b/1", "b")
    snap = snapshot(status("a", 0.15), status("b", 0.08))
    decision = decide(CHEAP, snap, [RouteView(a, weight=-20.0), RouteView(b)])
    assert ids(decision) == ["b/1", "a/1"]
    assert reason_of(decision, "a/1") == (
        "behind pace by 15 pp (week: 50% of time elapsed, 35% used, 50% left), weight −20"
    )
    assert reason_of(decision, "b/1") == _pace_reason(snap.pools["b"])


def test_negative_weight_never_helps_ahead_pool() -> None:
    a = make_route("a/1", "a")
    b = make_route("b/1", "b")
    snap = snapshot(status("a", -0.2), status("b", -0.05))
    decision = decide(CHEAP, snap, [RouteView(a, weight=-10.0), RouteView(b)])
    assert ids(decision) == ["b/1", "a/1"]


def test_priority_cuts_group() -> None:
    a = make_route("a/1", "a")
    b = make_route("b/1", "b")
    snap = snapshot(status("a", 0.5), status("b", -0.3))
    decision = decide(CHEAP, snap, [RouteView(a, priority=1), RouteView(b)])
    assert ids(decision) == ["b/1", "a/1"]
    assert reason_of(decision, "a/1").endswith(", priority 1")


def test_no_data_reason_gets_priority_but_not_weight() -> None:
    a = make_route("a/1", "a")
    decision = decide(CHEAP, snapshot(status("a", None)), [RouteView(a, priority=2, weight=-10.0)])
    assert reason_of(decision, "a/1") == f"{NO_DATA}, priority 2"


def group_routes():
    kimi = make_route("kimi/k", "kimi-code", 4, model_key="kimi-k3.1-pro")
    oc = make_route("oc/k", "opencode-go", 4, backend="opencode", model_key="kimi-k3.1-pro")
    other = make_route("oc/other", "other", 3, backend="opencode")
    return kimi, oc, other


def group_views(kimi, oc, other, **kimi_fields):
    return [
        RouteView(kimi, prefer_rank=0, **kimi_fields),
        RouteView(oc, prefer_rank=1),
        RouteView(other),
    ]


def test_prefer_is_absolute_inside_group() -> None:
    kimi, oc, other = group_routes()
    snap = snapshot(status("kimi-code", -0.2), status("opencode-go", 0.3), status("other", -0.4))
    decision = decide(CHEAP, snap, group_views(kimi, oc, other))
    assert ids(decision) == ["kimi/k", "oc/k", "oc/other"]
    assert reason_of(decision, "kimi/k").endswith(
        "; model kimi-k3.1-pro: pool kimi-code first by preference"
    )
    assert reason_of(decision, "oc/k").endswith(
        "; model kimi-k3.1-pro: pool opencode-go 2nd by preference"
    )
    assert reason_of(decision, "oc/other") == _pace_reason(snap.pools["other"])


def test_group_sorted_by_leader_key() -> None:
    kimi, oc, other = group_routes()
    # leader kimi-code is ahead of pace; the other model lags, so it goes first
    snap = snapshot(status("kimi-code", -0.2), status("opencode-go", 0.3), status("other", 0.1))
    decision = decide(CHEAP, snap, group_views(kimi, oc, other))
    assert ids(decision) == ["oc/other", "kimi/k", "oc/k"]


def test_leader_without_data_puts_group_in_tail() -> None:
    kimi, oc, other = group_routes()
    snap = snapshot(status("kimi-code", None), status("opencode-go", 0.3), status("other", 0.1))
    decision = decide(CHEAP, snap, group_views(kimi, oc, other))
    assert ids(decision) == ["oc/other", "kimi/k", "oc/k"]
    assert reason_of(decision, "kimi/k") == (
        f"{NO_DATA}; model kimi-k3.1-pro: pool kimi-code first by preference"
    )


def test_leader_excluded_next_member_leads() -> None:
    kimi, oc, other = group_routes()
    snap = snapshot(
        status("kimi-code", 0.4),
        status("opencode-go", 0.3),
        status("other", 0.1),
        blocks=("kimi-code",),
    )
    decision = decide(CHEAP, snap, group_views(kimi, oc, other))
    assert ids(decision) == ["oc/k", "oc/other"]
    excluded = {item.route.id: item.reason for item in decision.excluded}
    assert excluded["kimi/k"].startswith("pool closed until ")


def test_explicit_model_group_ignores_levers_and_reserve() -> None:
    kimi, oc, other = group_routes()
    kimi_status = replace(
        status("kimi-code", 0.1, reserve=0.2, reserve_tier="strong"), reserve_hit=True
    )
    snap = snapshot(
        kimi_status, status("opencode-go", 0.3), status("other", 0.5), blocks=("opencode-go",)
    )
    request = RouteRequest("cheap", model_group="kimi-k3.1-pro", explicit=True)
    decision = decide(request, snap, group_views(kimi, oc, other, weight=-50.0, priority=3))
    assert ids(decision) == ["kimi/k"]
    assert reason_of(decision, "kimi/k") == _pace_reason(kimi_status) + (
        "; model kimi-k3.1-pro: pool kimi-code first by preference"
    )
    excluded = {item.route.id for item in decision.excluded}
    assert excluded == {"oc/k"}


def test_explicit_route_ignores_levers() -> None:
    a = make_route("a/1", "a")
    snap = snapshot(status("a", 0.1))
    request = RouteRequest("cheap", explicit=True)
    decision = decide(request, snap, [RouteView(a, priority=5, weight=-10.0)])
    assert ids(decision) == ["a/1"]
    assert reason_of(decision, "a/1") == _pace_reason(snap.pools["a"])
    off = replace(a, enabled=False)
    decision = decide(request, snap, [RouteView(off, priority=5, weight=-10.0)])
    assert decision.chosen == ()
    assert [(item.route.id, item.reason) for item in decision.excluded] == [
        ("a/1", "route disabled")
    ]


def test_kind_filter() -> None:
    harness = make_route("h/1", "a")
    api = make_route("api/1", "a", backend=None, kind="api", upstream="x")
    api_off = make_route("api/2", "a", backend=None, kind="api", upstream="x", enabled=False)
    snap = snapshot(status("a", 0.1))
    views = [RouteView(harness), RouteView(api), RouteView(api_off)]
    decision = decide(CHEAP, snap, views)
    assert ids(decision) == ["h/1"]
    assert decision.excluded == ()
    decision = decide(RouteRequest("cheap", kind="api"), snap, views)
    assert ids(decision) == ["api/1"]
    assert [item.route.id for item in decision.excluded] == ["api/2"]


def test_model_group_filter_without_explicit() -> None:
    kimi, oc, other = group_routes()
    snap = snapshot(status("kimi-code", 0.1), status("opencode-go", 0.3), status("other", 0.5))
    request = RouteRequest("cheap", model_group="oc/other")
    decision = decide(request, snap, group_views(kimi, oc, other))
    assert ids(decision) == ["oc/other"]
    assert decision.excluded == ()


ROUTES = """
[[routes]]
id = "kimi/k"
pool = "kimi-code"
backend = "kimi"
model = "m"
tiers = ["cheap"]
model_key = "k"
weight = -7

[[routes]]
id = "oc/k"
pool = "opencode-go"
backend = "opencode"
model = "m"
tiers = ["cheap"]
model_key = "k"

[[routes]]
id = "oc/other"
pool = "opencode-go"
backend = "opencode"
model = "m"
tiers = ["cheap"]

[models."k"]
prefer = ["kimi-code", "opencode-go"]
"""


def test_route_views_fill_levers_and_prefer(home: Path) -> None:
    base = full_config_text().split("[[routes]]")[0]
    base = base.replace(
        '[backends.opencode]\nexecutable = ""\n',
        '[backends.opencode]\nexecutable = ""\npriority = 1\n',
    )
    config = write_config(home, base + ROUTES)
    views = {view.route.id: view for view in route_views(config)}
    assert (views["kimi/k"].priority, views["kimi/k"].weight, views["kimi/k"].prefer_rank) == (
        0,
        -7.0,
        0,
    )
    assert (views["oc/k"].priority, views["oc/k"].weight, views["oc/k"].prefer_rank) == (1, 0.0, 1)
    assert views["oc/other"].prefer_rank is None


@pytest.mark.parametrize("weight", [-10.0, 0.0, 10.0])
def test_single_route_group_order_independent_of_input(weight: float) -> None:
    a = make_route("a/1", "a")
    b = make_route("b/1", "b")
    snap = snapshot(status("a", 0.2), status("b", 0.05))
    forward = decide(CHEAP, snap, [RouteView(a, weight=weight), RouteView(b)])
    backward = decide(CHEAP, snap, [RouteView(b), RouteView(a, weight=weight)])
    assert ids(forward) == ids(backward)
