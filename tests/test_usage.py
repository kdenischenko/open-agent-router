"""Usage sources: observation mapping, pool/root usage tables, and the source registry."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_router import registry
from agent_router.config import ConfigError, load_config
from agent_router.config_model import Pool, PoolUsage, Window
from agent_router.periods import Duration
from agent_router.store import Observation
from agent_router.usage import UsagePoint, UsageRead, UsageSourceSpec, observations

NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)
RESET = datetime(2026, 12, 1, 0, tzinfo=UTC)

BASE = """\
[[pools]]
id = "pool"
title = "Pool"
unit = "percent"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "pace"

[[pools.windows]]
id = "5h"
duration = "5h"
kind = "rolling"
role = "guard"

[[routes]]
id = "route"
pool = "pool"
tiers = ["standard"]
quality = 3
"""


def fake_spec(fields: frozenset[str] = frozenset({"monthly", "rolling"})) -> UsageSourceSpec:
    return UsageSourceSpec(
        name="fake-usage",
        factory=lambda pool_usage, env: None,
        windows=fields,
        observation_source="fake_usage",
    )


def pool_with(windows: dict[str, str]) -> Pool:
    week = Window(
        id="week",
        duration=Duration(days=7),
        kind="fixed",
        role="pace",
        anchor=None,
        limit_usd=None,
        block_at=1.0,
    )
    guard = Window(
        id="5h",
        duration=Duration(hours=5),
        kind="rolling",
        role="guard",
        anchor=None,
        limit_usd=None,
        block_at=0.9,
    )
    return Pool(
        "p",
        "Pool",
        "percent",
        (guard, week),
        0.0,
        None,
        None,
        None,
        None,
        usage=PoolUsage(source="fake-usage", windows=windows),
    )


def with_pool_usage(body: str) -> str:
    """BASE config with a [pools.usage] sub-table after the pool's windows."""
    return BASE.replace("[[routes]]", "[pools.usage]\n" + body + "\n[[routes]]")


def load_text(tmp_path: Path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def fake_source(monkeypatch: pytest.MonkeyPatch) -> UsageSourceSpec:
    spec = fake_spec()
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)
    return spec


def test_observations_maps_fields_to_windows() -> None:
    read = UsageRead(
        points=(
            UsagePoint("monthly", 0.4, RESET),
            UsagePoint("rolling", 0.25),
        )
    )
    pool = pool_with({"monthly": "week", "rolling": "5h"})
    assert observations(pool, fake_spec(), read, NOW) == [
        Observation(
            pool="p",
            window="week",
            fraction=0.4,
            observed_at=NOW,
            resets_at=RESET,
            source="fake_usage",
        ),
        Observation(
            pool="p",
            window="5h",
            fraction=0.25,
            observed_at=NOW,
            resets_at=None,
            source="fake_usage",
        ),
    ]


def test_observations_drop_reset_time_on_rolling_windows() -> None:
    read = UsageRead(points=(UsagePoint("monthly", 0.4, RESET), UsagePoint("rolling", 0.25, RESET)))
    pool = pool_with({"monthly": "week", "rolling": "5h"})
    result = {item.window: item.resets_at for item in observations(pool, fake_spec(), read, NOW)}
    assert result == {"week": RESET, "5h": None}


def test_observations_skips_unmapped_fields() -> None:
    read = UsageRead(points=(UsagePoint("monthly", 0.4), UsagePoint("rolling", 0.25)))
    result = observations(pool_with({"monthly": "week"}), fake_spec(), read, NOW)
    assert [item.window for item in result] == ["week"]


@pytest.mark.parametrize("fraction", [1.5, -0.1, float("nan"), float("inf")])
def test_observations_drops_out_of_range_fractions(fraction: float) -> None:
    read = UsageRead(points=(UsagePoint("monthly", fraction),))
    assert observations(pool_with({"monthly": "week"}), fake_spec(), read, NOW) == []


def test_observations_empty_on_failed_read() -> None:
    read = UsageRead(points=(UsagePoint("monthly", 0.4),), reason="unauthorized")
    assert not read.ok
    assert observations(pool_with({"monthly": "week"}), fake_spec(), read, NOW) == []


def test_successful_read_is_ok() -> None:
    assert UsageRead(points=(UsagePoint("monthly", 0.4),)).ok


def test_unknown_usage_source_is_config_error(tmp_path: Path) -> None:
    text = with_pool_usage('source = "nope"\nwindows = { monthly = "week" }\n')
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, text)
    messages = caught.value.messages
    assert 'pools[0].usage.source: unknown usage source "nope"; available: opencode-go' in messages


def test_mapped_field_the_source_does_not_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", fake_spec(frozenset({"monthly"})))
    text = with_pool_usage('source = "fake-usage"\nwindows = { weekly = "week" }\n')
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, text)
    messages = caught.value.messages
    assert (
        "pools[0].usage.windows: source fake-usage has no field weekly; available: monthly"
        in messages
    )


def test_valid_fake_source_loads(tmp_path: Path, fake_source: UsageSourceSpec) -> None:
    text = with_pool_usage(
        'source = "fake-usage"\n'
        'api_key_env = "FAKE_USAGE_KEY"\n'
        'windows = { monthly = "week", rolling = "5h" }\n'
    )
    config = load_text(tmp_path, text)
    usage = config.pools[0].usage
    assert usage.source == "fake-usage"
    assert usage.api_key_env == "FAKE_USAGE_KEY"
    assert dict(usage.windows) == {"monthly": "week", "rolling": "5h"}
    assert registry.load_usage_source("fake-usage") is fake_source


def test_empty_usage_source_is_config_error(tmp_path: Path) -> None:
    text = with_pool_usage('source = ""\nwindows = { monthly = "week" }\n')
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, text)
    assert "pools[0].usage.source: must not be empty" in caught.value.messages
