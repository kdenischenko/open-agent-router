from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_router.config import Pool, Route, Window, load_config
from agent_router.ledger import WindowState
from agent_router.pacing import pool_status
from agent_router.periods import parse_duration
from agent_router.policy import (
    Decision,
    PoolBlock,
    RouteMetrics,
    RouteRequest,
    RouteView,
    Snapshot,
    decide,
)
from helpers import full_config_text

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TZ = timezone(timedelta(hours=3))
WEEK = Window("week", parse_duration("7d"), "fixed", "pace", None, None, 1.0)
GUARD = Window("5h", parse_duration("5h"), "rolling", "guard", None, None, 0.9)


def make_pool(id, reserve=0.0, reserve_tier=None):
    return Pool(id, id, "percent", (GUARD, WEEK), reserve, reserve_tier, None, None, None)


def week_state(pool, used, elapsed):
    start = NOW - timedelta(days=7) * elapsed
    return WindowState(
        pool, "week", True, used, "manual", NOW, start, start + timedelta(days=7), False, None
    )


def guard_state(pool, used, observed=NOW):
    return WindowState(pool, "5h", True, used, "manual", observed, None, None, False, None)


def status(id, used=None, elapsed=0.5, guard=None, **pool_fields):
    """Pool summary: used=None means no data; guard is the rolling window spend."""
    states = []
    if used is not None:
        states.append(week_state(id, used, elapsed))
    if guard is not None:
        states.append(guard_state(id, guard))
    return pool_status(make_pool(id, **pool_fields), states, NOW)


def route(
    id, pool, quality=3, tiers=("cheap", "standard", "strong"), modes=("read", "write"), **kw
):
    fields = {"backend": None, "model": None, "confirm": False, "enabled": True, "timeout_s": None}
    fields.update(kw)
    return Route(
        id=id,
        pool=pool,
        tiers=frozenset(tiers),
        quality=quality,
        modes=frozenset(modes),
        **fields,
    )


def snapshot(*statuses, blocks=()):
    return Snapshot(
        now=NOW,
        tz=TZ,
        pools={item.pool.id: item for item in statuses},
        blocks={block.pool: block for block in blocks},
    )


def views(*routes):
    return [RouteView(item) for item in routes]


def reasons(decision: Decision) -> dict[str, str]:
    return {item.route.id: item.reason for item in decision.excluded}


def ids(decision: Decision) -> list[str]:
    return [item.route.id for item in decision.chosen]


REQ = RouteRequest(tier="standard")


# --- exclusion reasons: exact strings --------------------------------------------


def test_disabled_route():
    decision = decide(REQ, snapshot(status("a", 0.2)), views(route("r", "a", enabled=False)))
    assert reasons(decision) == {"r": "route disabled"}
    assert decision.chosen == ()


def test_unavailable_harness():
    view = RouteView(route("r", "a"), available=False, unavailable_reason="kimi not found in PATH")
    decision = decide(REQ, snapshot(status("a", 0.2)), [view])
    assert reasons(decision) == {"r": "harness unavailable: kimi not found in PATH"}


def test_tier_not_served():
    decision = decide(REQ, snapshot(status("a", 0.2)), views(route("r", "a", tiers=("cheap",))))
    assert reasons(decision) == {"r": "does not serve tier standard"}


def test_mode_not_supported():
    request = RouteRequest(tier="standard", mode="write")
    decision = decide(request, snapshot(status("a", 0.2)), views(route("r", "a", modes=("read",))))
    assert reasons(decision) == {"r": "mode write not supported"}


def test_mode_none_does_not_filter_by_mode():
    decision = decide(REQ, snapshot(status("a", 0.2)), views(route("r", "a", modes=("read",))))
    assert ids(decision) == ["r"]


def test_excluded_route_and_excluded_pool():
    request = RouteRequest(
        tier="standard", excluded_routes=frozenset({"r1"}), excluded_pools=frozenset({"b"})
    )
    decision = decide(
        request,
        snapshot(status("a", 0.2), status("b", 0.2)),
        views(route("r1", "a"), route("r2", "a"), route("r3", "b")),
    )
    expected = {"r1": "excluded for this job", "r3": "excluded for this job"}
    assert reasons(decision) == expected
    assert ids(decision) == ["r2"]


def test_request_excluded_pool_has_advice_reason_and_other_route_is_chosen():
    request = RouteRequest(tier="standard", request_excluded_pools=frozenset({"a"}))

    decision = decide(
        request,
        snapshot(status("a", 0.2), status("b", 0.2)),
        views(route("r1", "a"), route("r2", "b")),
    )

    assert reasons(decision) == {"r1": "excluded by request"}
    assert ids(decision) == ["r2"]


def test_caller_pool_excluded_unless_explicit():
    snap = snapshot(status("a", 0.2))
    request = RouteRequest(tier="standard", caller_pool="a")
    assert reasons(decide(request, snap, views(route("r", "a")))) == {"r": "caller pool"}
    explicit = RouteRequest(tier="standard", caller_pool="a", explicit=True)
    assert ids(decide(explicit, snap, views(route("r", "a")))) == ["r"]


def test_block_reason_has_time_only_today():
    block = PoolBlock("a", datetime(2026, 11, 12, 6, tzinfo=UTC), "limit hit")
    snap = Snapshot(
        now=datetime(2026, 11, 12, 3, tzinfo=UTC),
        tz=TZ,
        pools={},
        blocks={"a": block},
    )
    decision = decide(REQ, snap, views(route("r", "a")))
    assert reasons(decision) == {"r": "pool closed until 09:00: limit hit"}


def test_block_reason_has_date_when_not_today():
    block = PoolBlock("a", datetime(2026, 11, 16, 6, tzinfo=UTC), "limit hit")
    decision = decide(REQ, snapshot(blocks=[block]), views(route("r", "a")))
    assert reasons(decision) == {"r": "pool closed until 16.11 09:00: limit hit"}


def test_expired_pool_block_is_ignored():
    block = PoolBlock("a", NOW, "limit hit")
    decision = decide(REQ, snapshot(status("a", 0.2), blocks=[block]), views(route("r", "a")))
    assert ids(decision) == ["r"]


def test_exhausted_window_with_time():
    decision = decide(REQ, snapshot(status("a", 0.2, guard=0.95)), views(route("r", "a")))
    # 12:00 UTC + 5 h = 20:00 +03
    assert reasons(decision) == {"r": "window 5h exhausted until 20:00"}


def test_exhausted_window_without_time():
    full = WindowState("a", "week", True, 1.0, "manual", NOW, None, None, False, None)
    snap = snapshot(pool_status(make_pool("a"), [full], NOW))
    assert reasons(decide(REQ, snap, views(route("r", "a")))) == {"r": "window week exhausted"}


def test_reserve_cuts_standard_but_not_strong():
    snap = snapshot(status("a", 0.86, elapsed=0.9, reserve=0.15, reserve_tier="strong"))
    routes = views(route("r", "a"))
    assert reasons(decide(REQ, snap, routes)) == {"r": "reserve 15% for strong tier jobs"}
    assert reasons(decide(RouteRequest(tier="cheap"), snap, routes)) == {
        "r": "reserve 15% for strong tier jobs"
    }
    assert ids(decide(RouteRequest(tier="strong"), snap, routes)) == ["r"]


def test_reserve_not_applied_to_explicit_route_or_below_threshold():
    hit = snapshot(status("a", 0.86, elapsed=0.9, reserve=0.15, reserve_tier="strong"))
    explicit = RouteRequest(tier="standard", explicit=True)
    assert ids(decide(explicit, hit, views(route("r", "a")))) == ["r"]
    free = snapshot(status("a", 0.84, elapsed=0.9, reserve=0.15, reserve_tier="strong"))
    assert ids(decide(REQ, free, views(route("r", "a")))) == ["r"]


def test_explicit_skips_tier_check_only_with_caller_and_reserve():
    snap = snapshot(status("a", 0.2, guard=0.95))
    explicit = RouteRequest(tier="strong", explicit=True, mode="write")
    cheap = route("r", "a", tiers=("cheap",), modes=("read",))
    assert reasons(decide(explicit, snap, views(cheap))) == {"r": "mode write not supported"}
    cheap_rw = route("r", "a", tiers=("cheap",))
    exhausted = {"r": "window 5h exhausted until 20:00"}
    assert reasons(decide(explicit, snap, views(cheap_rw))) == exhausted


def test_first_matching_check_gives_the_reason():
    """The route violates every rule at once: each blocking reason is removed one at a time
    in the order the checks run."""
    block = PoolBlock("a", NOW + timedelta(hours=1), "limit hit")
    snap = snapshot(
        status("a", 0.95, elapsed=0.9, guard=0.95, reserve=0.15, reserve_tier="strong"),
        blocks=[block],
    )
    request = RouteRequest(
        tier="standard", mode="write", caller_pool="a", excluded_routes=frozenset({"r"})
    )
    bad = {"tiers": ("cheap",), "modes": ("read",), "enabled": False}
    down = {"available": False, "unavailable_reason": "none"}
    expected = [
        (bad, down, request, snap, "route disabled"),
        (bad | {"enabled": True}, down, request, snap, "harness unavailable: none"),
        (bad | {"enabled": True}, {}, request, snap, "does not serve tier standard"),
        ({"modes": ("read",)}, {}, request, snap, "mode write not supported"),
        ({}, {}, request, snap, "excluded for this job"),
        ({}, {}, RouteRequest(tier="standard", caller_pool="a"), snap, "caller pool"),
        ({}, {}, REQ, snap, "pool closed until 16:00: limit hit"),
        ({}, {}, REQ, snapshot(snap.pools["a"]), "window 5h exhausted until 20:00"),
    ]
    for route_fields, view_fields, req, snap_now, reason in expected:
        view = RouteView(route("r", "a", **route_fields), **view_fields)
        assert reasons(decide(req, snap_now, [view])) == {"r": reason}


def test_review_excludes_author_model_group_and_pool():
    request = RouteRequest(tier="standard", reviewed_model_group="mk-a", reviewed_pool="pool-a")
    decision = decide(
        request,
        snapshot(status("pool-a", 0.2), status("pool-b", 0.2)),
        views(
            route("r-author", "pool-a", model_key="mk-a"),
            route("r-same-model", "pool-b", model_key="mk-a"),
            route("r-same-pool", "pool-a", model_key="mk-b"),
            route("r-other", "pool-b", model_key="mk-b"),
        ),
    )
    assert reasons(decision) == {
        "r-author": "author of reviewed job",
        "r-same-model": "author of reviewed job",
        "r-same-pool": "author of reviewed job",
    }
    assert ids(decision) == ["r-other"]
    assert decision.note is None


def test_review_exclusion_skipped_for_explicit_request():
    request = RouteRequest(
        tier="standard", explicit=True, reviewed_model_group="mk-a", reviewed_pool="pool-a"
    )
    snap = snapshot(status("pool-a", 0.2))
    assert ids(decide(request, snap, views(route("r", "pool-a", model_key="mk-a")))) == ["r"]


def test_review_falls_back_to_author_model_with_note():
    request = RouteRequest(tier="standard", reviewed_model_group="mk-a", reviewed_pool="pool-a")
    snap = snapshot(status("pool-a", 0.2))
    decision = decide(request, snap, views(route("r-author", "pool-a", model_key="mk-a")))
    assert ids(decision) == ["r-author"]
    assert decision.note == "review runs on the author's model: nothing else was eligible"
    assert reasons(decision) == {}


def test_review_fallback_keeps_original_decision_when_retry_also_empty():
    # the author's route does not serve the tier: dropping the author exclusion changes nothing
    request = RouteRequest(tier="strong", reviewed_model_group="mk-a", reviewed_pool="pool-a")
    snap = snapshot(status("pool-a", 0.2))
    decision = decide(
        request, snap, views(route("r-author", "pool-a", model_key="mk-a", tiers=("cheap",)))
    )
    assert decision.chosen == ()
    assert decision.note is None
    assert reasons(decision) == {"r-author": "author of reviewed job"}


# --- chosen routes: order and reasons --------------------------------------------


def test_reason_for_lagging_and_leading_pool():
    snap = snapshot(status("a", 0.38, elapsed=0.61), status("b", 0.80, elapsed=0.50))
    decision = decide(REQ, snap, views(route("ra", "a"), route("rb", "b")))
    assert [(item.route.id, item.reason) for item in decision.chosen] == [
        ("ra", "behind pace by 23 pp (week: 61% of time elapsed, 38% used, 39% left)"),
        ("rb", "ahead of pace by 30 pp (week: 50% of time elapsed, 80% used, 50% left)"),
    ]


def test_order_by_lag_then_quality_then_id():
    snap = snapshot(status("a", 0.10), status("b", 0.30), status("c", 0.30))
    routes = views(
        route("z-low", "b", quality=2),
        route("m-high", "c", quality=4),
        route("b-high", "b", quality=4),
        route("lagging", "a", quality=1),
    )
    assert ids(decide(REQ, snap, routes)) == ["lagging", "b-high", "m-high", "z-low"]


def test_permutation_of_input_does_not_change_result():
    block = PoolBlock("d", NOW + timedelta(hours=1), "limit hit")
    snap = snapshot(
        status("a", 0.10), status("b", 0.30), status("c"), status("d", 0.1), blocks=[block]
    )
    routes = views(
        route("r1", "a", quality=2),
        route("r2", "b", quality=5),
        route("r3", "c", quality=4),
        route("r4", "c", quality=4, enabled=False),
        route("r5", "d"),
        route("r6", "b", quality=5),
    )
    base = decide(REQ, snap, routes)
    assert ids(base) == ["r1", "r2", "r6", "r3"]
    assert [item.route.id for item in base.excluded] == ["r4", "r5"]
    for shuffled in itertools.permutations(routes):
        assert decide(REQ, snap, list(shuffled)) == base


def test_pool_without_data_goes_last():
    snap = snapshot(status("a", 0.90, elapsed=0.10), status("b"))
    routes = views(
        route("nodata-best", "b", quality=5), route("nodata", "b"), route("ahead", "a", quality=1)
    )
    decision = decide(REQ, snap, routes)
    assert ids(decision) == ["ahead", "nodata-best", "nodata"]
    assert decision.chosen[1].reason == "no data, calibrate"
    assert decision.chosen[2].reason == "no data, calibrate"


def test_pool_missing_from_snapshot_counts_as_no_data():
    decision = decide(REQ, snapshot(), views(route("r", "ghost")))
    assert [(item.route.id, item.reason) for item in decision.chosen] == [
        ("r", "no data, calibrate")
    ]


# --- decision metrics -------------------------------------------------------------


def test_decide_metrics_match_sort_key():
    """Metrics of chosen routes are the same values used as the sort key."""
    snap = snapshot(status("a", 0.25, elapsed=0.5), status("b", 0.75, elapsed=0.5))
    routes = [
        RouteView(route("ra", "a"), priority=1, weight=5.0),
        RouteView(route("rb", "b"), prefer_rank=0),
        RouteView(route("rc", "c")),
    ]
    decision = decide(REQ, snap, routes)
    assert ids(decision) == ["rb", "rc", "ra"]
    assert set(decision.metrics) == set(ids(decision))
    assert decision.metrics["ra"] == RouteMetrics(
        pool="a",
        priority=1,
        weight=5.0,
        lag=0.25,
        score=55.0,
        quality=3,
        has_data=True,
        prefer_rank=None,
        urgency=0.5,
    )
    assert decision.metrics["rb"] == RouteMetrics(
        pool="b",
        priority=0,
        weight=0.0,
        lag=-0.25,
        score=-50.0,
        quality=3,
        has_data=True,
        prefer_rank=0,
        urgency=-0.5,
    )
    assert decision.metrics["rc"] == RouteMetrics(
        pool="c",
        priority=0,
        weight=0.0,
        lag=None,
        score=0.0,
        quality=3,
        has_data=False,
        prefer_rank=None,
        urgency=None,
    )


# --- benchmark --------------------------------------------------------------------


@pytest.mark.bench
def test_bench_decide(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(full_config_text(), encoding="utf-8")
    config = load_config(path)
    statuses = {}
    for index, pool in enumerate(config.pools):
        states = []
        for window in pool.windows:
            length = timedelta(hours=5) if window.id == "5h" else timedelta(days=7)
            start = NOW - length / 2
            states.append(
                WindowState(
                    pool.id,
                    window.id,
                    True,
                    0.2 + 0.1 * index,
                    "manual",
                    NOW,
                    start if window.kind == "fixed" else None,
                    start + length if window.kind == "fixed" else None,
                    False,
                    None,
                )
            )
        statuses[pool.id] = pool_status(pool, states, NOW)
    snap = Snapshot(now=NOW, tz=TZ, pools=statuses, blocks={})
    routes = [RouteView(item) for item in config.routes]
    request = RouteRequest(tier="standard", caller_pool="claude-pro")
    for _ in range(200):
        decide(request, snap, routes)
    started = time.perf_counter()
    for _ in range(1000):
        decision = decide(request, snap, routes)
    mean_ms = (time.perf_counter() - started) / 1000 * 1000
    assert decision.chosen
    # benchmark output is ASCII: a redirected Windows console can't handle Cyrillic
    print(f"\nbench decide: mean {mean_ms:.4f} ms per call (target 1 ms)")


@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        (0, "first"),
        (1, "2nd"),
        (2, "3rd"),
        (3, "4th"),
        (10, "11th"),
        (11, "12th"),
        (12, "13th"),
        (20, "21st"),
        (21, "22nd"),
        (22, "23rd"),
        (110, "111th"),
    ],
)
def test_preference_rank_ordinal(rank: int, expected: str) -> None:
    from agent_router.policy import _ordinal

    assert _ordinal(rank) == expected


def test_pool_close_to_its_reset_outranks_a_pool_with_more_time():
    # 86% used with 3% of the window left beats 36% used with 45% left, despite a cost weight
    near = status("near", 0.86, elapsed=0.97)
    far = status("far", 0.36, elapsed=0.55)
    routes = [RouteView(route("r-near", "near"), weight=-13.5), RouteView(route("r-far", "far"))]
    decision = decide(REQ, snapshot(near, far), routes)
    assert ids(decision) == ["r-near", "r-far"]
    assert decision.metrics["r-near"].score == pytest.approx(100 * 0.11 / 0.03 - 13.5)
    assert decision.metrics["r-far"].score == pytest.approx(100 * 0.19 / 0.45)


def test_reason_names_the_time_left_in_the_window():
    decision = decide(REQ, snapshot(status("a", 0.86, elapsed=0.97)), views(route("ra", "a")))
    assert decision.chosen[0].reason == (
        "behind pace by 11 pp (week: 97% of time elapsed, 86% used, 3% left)"
    )
