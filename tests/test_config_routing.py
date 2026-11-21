"""Config tests: route kinds, models, levers, API upstreams, spend source."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_router.config import (
    ClassifierSettings,
    Config,
    ConfigError,
    Pool,
    Route,
    StorageSettings,
    TaskSettings,
    default_config_text,
)
from helpers import ALL_HARNESSES, write_config

# template without routes: the tested route is always routes[0]
BASE = default_config_text(ALL_HARNESSES).split("[[routes]]")[0]

UPSTREAM = """
[upstreams.x]
kind = "openai"
pool = "opencode-go"
base_url = "https://example.invalid/v1"
key_env = "X_KEY"
"""


def harness_route(extra: str = "", *, backend: str = "opencode", pool: str = "opencode-go") -> str:
    return f"""
[[routes]]
id = "h/m"
pool = "{pool}"
backend = "{backend}"
model = "m"
tiers = ["cheap"]
{extra}
"""


def api_route(extra: str = "") -> str:
    return f"""
[[routes]]
id = "api/m"
kind = "api"
pool = "opencode-go"
model = "glm-5.3"
tiers = ["cheap"]
{extra}
"""


def config_error(home: Path, text: str) -> str:
    with pytest.raises(ConfigError) as caught:
        write_config(home, text)
    return str(caught.value)


def minimal_config_text() -> str:
    """Default template with cline route, [models], model_key and new callers removed."""
    template = default_config_text(ALL_HARNESSES)
    text = template.split("\n# Cline CLI on the OpenCode Go pool")[0] + "\n"
    for line in (
        'cline = "opencode-go"\n',
        'opencode = "opencode-go"\n',
        '[backends.cline]\nexecutable = ""\n\n',
        'model_key = "kimi-k3.1-pro"\n',
        'model_key = "glm-5.3"\n',
    ):
        assert line in text
        text = text.replace(line, "", 1)
    return text


def test_minimal_config_still_loads(home: Path) -> None:
    config = write_config(home, minimal_config_text())
    assert config.routes
    for route in config.routes:
        assert route.kind == "harness"
        assert route.upstream is None
        assert route.model_group == route.id
        assert config.levers(route) == (0, 0.0)
    assert config.models == {}
    assert config.upstreams == {}
    assert config.callers == {"claude-code": "claude-pro", "kimi-code": "kimi-code"}
    spend = {route.backend: config.route_spend(route) for route in config.routes}
    assert spend == {"claude": "journal", "kimi": "journal", "opencode": "counter"}


def test_old_constructors_still_work() -> None:
    route = Route(
        id="r",
        pool="p",
        tiers=frozenset({"cheap"}),
        quality=3,
        backend="kimi",
        model=None,
        modes=frozenset({"read"}),
        confirm=False,
        enabled=True,
        timeout_s=None,
    )
    assert (route.kind, route.model_group, route.priority, route.weight) == (
        "harness",
        "r",
        None,
        None,
    )
    pool = Pool("p", "p", "percent", (), 0.0, None, None, None, None)
    assert (pool.priority, pool.weight, pool.price_file) == (None, None, None)
    config = Config(
        pools=(pool,),
        routes=(route,),
        callers={},
        tasks=TaskSettings(allowed_roots=(), denied_roots=()),
        backends={},
        statusline=None,
        classifier=ClassifierSettings(),
        storage=StorageSettings(),
        sinks=(),
    )
    assert config.models == {}
    assert config.upstreams == {}
    assert config.levers(route) == (0, 0.0)


def levers_text(*, route: bool, section: bool) -> str:
    text = BASE.replace(
        'counter_provider = "opencode-go"\n',
        'counter_provider = "opencode-go"\npriority = 3\nweight = -5\n',
    )
    if section:
        text = text.replace(
            '[backends.opencode]\nexecutable = ""\n',
            '[backends.opencode]\nexecutable = ""\npriority = 2\nweight = -10\n',
        )
    return text + harness_route("priority = 1\nweight = 3" if route else "")


@pytest.mark.parametrize(
    ("route", "section", "expected"),
    [(True, True, (1, 3.0)), (False, True, (2, -10.0)), (False, False, (3, -5.0))],
)
def test_levers_inheritance(home: Path, route: bool, section: bool, expected) -> None:
    config = write_config(home, levers_text(route=route, section=section))
    assert config.levers(config.routes[0]) == expected


def test_levers_zero_on_lower_level_overrides(home: Path) -> None:
    text = levers_text(route=False, section=True).replace("weight = -10", "weight = 0")
    config = write_config(home, text)
    assert config.levers(config.routes[0]) == (2, 0.0)


def test_weight_must_be_finite(home: Path) -> None:
    message = config_error(home, BASE + harness_route("weight = inf"))
    assert "routes[0].weight: expected a finite number" in message


def test_models_prefer(home: Path) -> None:
    text = (
        BASE
        + harness_route('model_key = "k"')
        + '\n[models."k"]\nprefer = ["kimi-code", "opencode-go"]\n'
    )
    config = write_config(home, text)
    assert config.routes[0].model_key == "k"
    assert config.routes[0].model_group == "k"
    assert config.models["k"].prefer == ("kimi-code", "opencode-go")


def test_models_prefer_unknown_pool(home: Path) -> None:
    message = config_error(home, BASE + '\n[models."k"]\nprefer = ["nope"]\n')
    assert 'models."k".prefer: unknown pool nope' in message


def test_models_prefer_repeated_pool(home: Path) -> None:
    message = config_error(home, BASE + '\n[models."k"]\nprefer = ["kimi-code", "kimi-code"]\n')
    assert 'models."k".prefer: pool kimi-code is repeated' in message


def test_api_route_is_rejected(home: Path) -> None:
    message = config_error(home, BASE + api_route('upstream = "x"') + UPSTREAM)
    assert "routes[0].kind: API proxy is not implemented yet" in message
    assert "upstreams: API proxy is not implemented yet, remove the section" in message


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ('upstream = "x"\nbackend = "kimi"', "routes[0]: an api route has no backend"),
        ('upstream = "x"\nmodes = ["read"]', "routes[0]: an api route has no modes"),
        ("", "routes[0].upstream: required for an api route"),
        ('upstream = "nope"', "routes[0].upstream: unknown upstream: 'nope'"),
    ],
)
def test_api_route_validation(home: Path, extra: str, expected: str) -> None:
    assert expected in config_error(home, BASE + api_route(extra) + UPSTREAM)


def test_harness_route_has_no_upstream(home: Path) -> None:
    message = config_error(home, BASE + harness_route('upstream = "x"') + UPSTREAM)
    assert "routes[0]: a harness route has no upstream" in message


def test_unknown_route_kind(home: Path) -> None:
    message = config_error(home, BASE + harness_route('kind = "nope"'))
    assert "routes[0].kind: expected harness or api" in message


def test_pass_headers_denied(home: Path) -> None:
    text = BASE + UPSTREAM + 'pass_headers = ["x-opencode-session", "Authorization"]\n'
    message = config_error(home, text)
    assert "upstreams.x.pass_headers: header authorization must not be passed" in message


def test_pass_headers_allowed(home: Path) -> None:
    text = BASE + UPSTREAM + 'pass_headers = ["x-opencode-session"]\n'
    message = config_error(home, text)
    assert "must not be passed" not in message
    assert "upstreams: API proxy is not implemented yet, remove the section" in message


@pytest.mark.parametrize(
    "keys", ['key_file = "x.key"\nkey_env = "X_KEY"', ""], ids=["both", "none"]
)
def test_upstream_needs_exactly_one_key_source(home: Path, keys: str) -> None:
    upstream = UPSTREAM.replace('key_env = "X_KEY"\n', keys + "\n")
    message = config_error(home, BASE + upstream)
    assert "upstreams.x: exactly one of key_file and key_env is required" in message


def test_upstream_unknown_pool(home: Path) -> None:
    message = config_error(home, BASE + UPSTREAM.replace('pool = "opencode-go"', 'pool = "nope"'))
    assert "upstreams.x.pool: unknown pool: 'nope'" in message


def test_callers_empty_pool(home: Path) -> None:
    text = default_config_text(ALL_HARNESSES).replace('cline = "opencode-go"', 'cline = ""')
    config = write_config(home, text)
    assert config.callers["cline"] is None
    assert config.callers["kimi-code"] == "kimi-code"
    bad = text.replace('cline = ""', 'cline = "nope"')
    assert "callers.cline: unknown pool: 'nope'" in config_error(home, bad)


def test_spend_counter_requires_seeing_counter(home: Path) -> None:
    message = config_error(home, BASE + harness_route('spend = "counter"', backend="kimi"))
    assert "routes[0].spend: counter opencode_stats does not see harness kimi" in message


def test_spend_counter_requires_pool_counter(home: Path) -> None:
    text = BASE + harness_route('spend = "counter"', backend="kimi", pool="kimi-code")
    assert "routes[0].spend: pool kimi-code has no counter" in config_error(home, text)


def test_spend_proxy_requires_proxy_capable_harness(home: Path) -> None:
    message = config_error(home, BASE + harness_route('spend = "proxy"', backend="kimi"))
    assert "routes[0].spend: harness kimi does not work through the proxy" in message


def test_spend_unknown_value(home: Path) -> None:
    message = config_error(home, BASE + harness_route('spend = "nope"'))
    assert "routes[0].spend: expected counter, journal, or proxy" in message


def test_route_spend_override_and_default(home: Path) -> None:
    config = write_config(home, BASE + harness_route('spend = "journal"'))
    assert config.routes[0].spend == "journal"
    assert config.route_spend(config.routes[0]) == "journal"
    config = write_config(home, BASE + harness_route())
    assert config.routes[0].spend is None
    assert config.route_spend(config.routes[0]) == "counter"


def test_pool_price_file(home: Path) -> None:
    text = BASE.replace(
        'counter_provider = "opencode-go"\n',
        'counter_provider = "opencode-go"\nprice_file = "prices.toml"\n',
    )
    home.mkdir(parents=True, exist_ok=True)
    (home / "prices.toml").write_text('"glm-5.3" = [0.5, 2.0]\n', encoding="utf-8")
    config = write_config(home, text)
    pool = next(pool for pool in config.pools if pool.id == "opencode-go")
    assert pool.price_file == home / "prices.toml"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("modes = []", "routes[0].modes: must not be empty"),
        ("", "routes[0].tiers: must not be empty"),
    ],
)
def test_route_lists_must_not_be_empty(home: Path, extra: str, expected: str) -> None:
    route = harness_route(extra)
    if not extra:
        route = route.replace('tiers = ["cheap"]', "tiers = []")
    assert expected in config_error(home, BASE + route)
