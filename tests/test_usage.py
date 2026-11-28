"""Usage sources: observation mapping, pool/root usage tables, and the source registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_router import registry
from agent_router.config import ConfigError, load_config
from agent_router.config_model import Pool, PoolUsage, Window
from agent_router.periods import Duration
from agent_router.store import Observation
from agent_router.usage import UsagePoint, UsageRead, UsageSourceSpec, is_stale, observations
from agent_router.usage_reader import UsageReadState

NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)
RESET = datetime(2026, 11, 16, 0, tzinfo=UTC)  # within one week window of NOW

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
backend = "kimi"
model = "m"
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


# --- staleness: one threshold shared by web, status and doctor --------------------------


def test_is_stale_is_false_right_after_a_success() -> None:
    state = UsageReadState("p", last_attempt_at=NOW, last_success_at=NOW)
    assert is_stale(state, NOW, 300) is False


def test_is_stale_turns_true_only_past_two_intervals() -> None:
    state = UsageReadState("p", last_attempt_at=NOW, last_success_at=NOW)
    assert is_stale(state, NOW + timedelta(seconds=600), 300) is False
    assert is_stale(state, NOW + timedelta(seconds=601), 300) is True


def test_is_stale_when_never_read() -> None:
    assert is_stale(None, NOW, 300) is True
    assert is_stale(UsageReadState("p"), NOW, 300) is True


def test_shipped_source_names_its_default_key_variable() -> None:
    spec = registry.load_usage_source("opencode-go")
    assert spec.default_api_key_env == "OPENCODE_GO_API_KEY"


def test_unknown_usage_source_is_config_error(tmp_path: Path) -> None:
    text = with_pool_usage('source = "nope"\nwindows = { monthly = "week" }\n')
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, text)
    messages = caught.value.messages
    expected = 'unknown usage source "nope"; available: claude-code, codex, opencode-go'
    assert f"pools[0].usage.source: {expected}" in messages


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


@pytest.mark.parametrize(
    "resets_at",
    [NOW - timedelta(minutes=1), NOW, NOW + timedelta(days=8)],
    ids=["past", "now", "beyond-one-window"],
)
def test_observations_drop_implausible_reset_times(resets_at: datetime) -> None:
    read = UsageRead(points=(UsagePoint("monthly", 0.4, resets_at),))
    [item] = observations(pool_with({"monthly": "week"}), fake_spec(), read, NOW)
    assert item.resets_at is None
    assert item.fraction == 0.4  # the figure itself is still recorded
