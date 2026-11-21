"""Policy scenario table: pool snapshot, controls, request -> order.

Add a scenario as a `pytest.param(Scenario(...), id=...)` row in SCENARIOS:
routes and controls are TOML.
`weight` is an offset in percentage points of lag: -10 means "pick it if the pool
trails the others by 10 pp more."
When tuning a weight, add a row with your pools' lags and check where it lands in `expected`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_router.ledger import WindowState
from agent_router.pacing import pool_status
from agent_router.policy import PoolBlock, RouteRequest, Snapshot, decide
from agent_router.service import route_views
from helpers import write_config

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TZ = timezone(timedelta(hours=3))
STANDARD = RouteRequest("standard")


def pool_toml(pool_id: str, extra: str = "") -> str:
    return f"""
[[pools]]
id = "{pool_id}"
title = "{pool_id}"
unit = "percent"
{extra}
  [[pools.windows]]
  id = "week"
  duration = "7d"
  kind = "fixed"
  role = "pace"
"""


# shared header: three pools with a single week window; kimi-code holds a strong-tier reserve
HEADER = (
    "[tasks]\nallowed_roots = []\n"
    + pool_toml("kimi-code", 'reserve = 0.15\nreserve_tier = "strong"')
    + pool_toml("opencode-go")
    + pool_toml("claude-pro")
)


def route_toml(route_id: str, pool: str, quality: int, extra: str = "", backend: str = "kimi"):
    return f"""
[[routes]]
id = "{route_id}"
pool = "{pool}"
backend = "{backend}"
model = "m"
tiers = ["standard"]
quality = {quality}
modes = ["read", "write"]
{extra}
"""


K3 = 'model_key = "kimi-k3.1-pro"'
PREFER = '\n[models."kimi-k3.1-pro"]\nprefer = ["kimi-code", "opencode-go"]\n'
KIMI_K3 = route_toml("kimi/k3", "kimi-code", 4, K3)
OC_K3 = route_toml("oc/k3", "opencode-go", 4, K3)
OC_GLM = route_toml("oc/glm", "opencode-go", 3)
CLAUDE = route_toml("claude/sonnet", "claude-pro", 5, backend="claude")
GROUP = KIMI_K3 + OC_K3 + PREFER


@dataclass(frozen=True)
class Scenario:
    name: str
    config_toml: str
    lags: Mapping[str, float | None]  # pool -> lag (fraction); missing pool or None means no data
    expected: tuple[str, ...]  # route ids of `chosen` in order
    blocks: frozenset[str] = frozenset()
    reserve_hit: frozenset[str] = frozenset()
    request: RouteRequest = field(default=STANDARD)


def case(scenario: Scenario):
    return pytest.param(scenario, id=scenario.name)


SCENARIOS = [
    case(
        Scenario(
            "neutral-levers",
            route_toml("kimi/k3", "kimi-code", 4) + OC_GLM + CLAUDE,
            {"kimi-code": 0.05, "opencode-go": 0.10, "claude-pro": None},
            ("oc/glm", "kimi/k3", "claude/sonnet"),
        )
    ),
    case(
        Scenario(
            "kimi-model-goes-kimi-code-while-opencode-lags",
            GROUP,
            {"kimi-code": 0.0, "opencode-go": 0.20},
            ("kimi/k3", "oc/k3"),
        )
    ),
    case(
        Scenario(
            "kimi-code-blocked-same-model-before-other-model",
            GROUP + OC_GLM,
            {"kimi-code": 0.30, "opencode-go": 0.0},
            ("oc/k3", "oc/glm"),
            blocks=frozenset({"kimi-code"}),
        )
    ),
    case(
        Scenario(
            "expensive-model-weight-minus-10",
            route_toml("claude/sonnet", "claude-pro", 5, "weight = -10", backend="claude") + OC_GLM,
            {"claude-pro": 0.15, "opencode-go": 0.08},
            ("oc/glm", "claude/sonnet"),  # without the weight Claude (15 pp) would come first
        )
    ),
    case(
        Scenario(
            "priority-cuts-group",
            CLAUDE + OC_GLM + '\n[backends.claude]\nexecutable = ""\npriority = 1\n',
            {"claude-pro": 0.30, "opencode-go": 0.0},
            ("oc/glm", "claude/sonnet"),  # harness section priority outweighs the lag
        )
    ),
    case(
        Scenario(
            "ahead-pool-negative-weight-not-better",
            route_toml("claude/sonnet", "claude-pro", 5, "weight = -10", backend="claude") + OC_GLM,
            {"claude-pro": -0.05, "opencode-go": -0.10},
            ("oc/glm", "claude/sonnet"),  # -5-10 = -15, worse than -10; Claude leads unweighted
        )
    ),
    case(
        Scenario(
            "prefer-no-data-fallback-has-data",
            GROUP + CLAUDE,
            {"kimi-code": None, "opencode-go": 0.20, "claude-pro": 0.0},
            ("claude/sonnet", "kimi/k3", "oc/k3"),  # leader has no data, so its group goes last
        )
    ),
    case(
        Scenario(
            "leader-reserve-next-leads",
            GROUP + OC_GLM + CLAUDE,
            {"kimi-code": 0.30, "opencode-go": 0.0, "claude-pro": 0.10},
            ("claude/sonnet", "oc/k3", "oc/glm"),  # group follows its new leader oc/k3
            reserve_hit=frozenset({"kimi-code"}),
        )
    ),
    case(
        Scenario(
            "leader-caller-pool-next-leads",
            GROUP + OC_GLM + CLAUDE,
            {"kimi-code": 0.30, "opencode-go": 0.0, "claude-pro": 0.10},
            ("claude/sonnet", "oc/k3", "oc/glm"),
            request=RouteRequest("standard", caller_pool="kimi-code"),
        )
    ),
    case(
        Scenario(
            "members-differ-in-priority-and-weight",
            route_toml("kimi/k3", "kimi-code", 4, K3 + "\nweight = -20")
            + route_toml("oc/k3", "opencode-go", 4, K3 + "\npriority = 1")
            + PREFER
            + OC_GLM,
            {"kimi-code": 0.25, "opencode-go": 0.10},
            ("oc/glm", "kimi/k3", "oc/k3"),  # group is placed by its leader (25-20 = 5 < 10)
        )
    ),
]


def snapshot_of(config, scenario: Scenario) -> Snapshot:
    pools = {}
    start = NOW - timedelta(days=3.5)
    for pool in config.pools:
        lag = scenario.lags.get(pool.id)
        states = []
        if lag is not None:
            end = start + timedelta(days=7)
            states.append(
                WindowState(
                    pool.id, "week", True, 0.5 - lag, "manual", NOW, start, end, False, None
                )
            )
        status = pool_status(pool, states, NOW)
        if pool.id in scenario.reserve_hit:
            status = replace(status, reserve_hit=True)
        pools[pool.id] = status
    blocks = {pool: PoolBlock(pool, NOW + timedelta(hours=2), "limit") for pool in scenario.blocks}
    return Snapshot(now=NOW, tz=TZ, pools=pools, blocks=blocks)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario(home: Path, scenario: Scenario) -> None:
    config = write_config(home, HEADER + scenario.config_toml)
    decision = decide(scenario.request, snapshot_of(config, scenario), route_views(config))
    assert tuple(choice.route.id for choice in decision.chosen) == scenario.expected
