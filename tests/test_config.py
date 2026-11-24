"""Configuration loading and validation."""

from __future__ import annotations

import importlib
import importlib.resources
import json
import tomllib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_router.config_model import PoolUsage
from helpers import ALL_HARNESSES

VALID_CONFIG = """\
[[pools]]
id = "pool"
title = "Pool"
unit = "percent"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "pace"

[[routes]]
id = "route"
pool = "pool"
backend = "kimi"
model = "m"
tiers = ["standard"]
quality = 3
"""


@pytest.fixture
def config_module():
    return importlib.import_module("agent_router.config")


def load_text(tmp_path: Path, module, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return module.load_config(path)


@pytest.fixture
def default_config(tmp_path: Path, config_module):
    text = config_module.default_config_text(ALL_HARNESSES)
    return load_text(tmp_path, config_module, text)


def test_default_template_loads_three_pools(default_config) -> None:
    assert len(default_config.pools) == 3
    assert {pool.id for pool in default_config.pools} == {
        "claude-pro",
        "kimi-code",
        "opencode-go",
    }


def test_claude_route_is_enabled_and_requires_confirmation(default_config) -> None:
    route = next(route for route in default_config.routes if route.backend == "claude")
    assert route.enabled is True
    assert route.confirm is True


def test_default_template_has_no_sinks(default_config) -> None:
    assert default_config.sinks == ()


def test_default_template_uses_sqlite(default_config) -> None:
    assert default_config.storage.backend == "sqlite"


def test_route_to_unknown_pool_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('pool = "pool"', 'pool = "missing"')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_duplicate_route_id_is_rejected(tmp_path: Path, config_module) -> None:
    text = (
        VALID_CONFIG
        + """\

[[routes]]
id = "route"
pool = "pool"
backend = "kimi"
model = "m"
tiers = ["cheap"]
quality = 2
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_usd_window_requires_limit(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('unit = "percent"', 'unit = "usd"')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


@pytest.mark.parametrize("reserve", [-0.01, 1.01])
def test_reserve_outside_unit_interval_is_rejected(
    tmp_path: Path, config_module, reserve: float
) -> None:
    text = VALID_CONFIG.replace('unit = "percent"', f'unit = "percent"\nreserve = {reserve}')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_numbers_are_rejected(tmp_path: Path, config_module, value: str) -> None:
    """TOML allows nan and inf; checks like "not less than zero" let them through."""
    text = VALID_CONFIG + f"\n[server]\nsnapshot_ttl_s = {value}\n"
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_unknown_route_tier_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('tiers = ["standard"]', 'tiers = ["unknown"]')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_bad_window_duration_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('duration = "7d"', 'duration = "seven days"')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_caller_to_unknown_pool_is_rejected(tmp_path: Path, config_module) -> None:
    text = (
        VALID_CONFIG
        + """\

[callers]
unknown-caller = "missing"
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_rolling_pace_window_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('kind = "fixed"', 'kind = "rolling"')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_pool_without_pace_window_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('role = "pace"', 'role = "guard"')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


@pytest.mark.parametrize("character", ["&", "|", "^", "%", '"', "\n", "\r"])
def test_cmd_unsafe_model_is_rejected(tmp_path: Path, config_module, character: str) -> None:
    model = json.dumps(f"bad{character}model")
    text = VALID_CONFIG.replace('model = "m"', f"model = {model}")
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


@pytest.mark.parametrize("character", ["&", "|", "^", "%", '"', "\n", "\r"])
def test_cmd_unsafe_executable_is_rejected(tmp_path: Path, config_module, character: str) -> None:
    executable = json.dumps(f"bad{character}executable")
    text = (
        VALID_CONFIG
        + f"""\

[backends.bad]
executable = {executable}
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_sink_without_type_is_rejected(tmp_path: Path, config_module) -> None:
    text = (
        VALID_CONFIG
        + """\

[[sinks]]
path = "events.jsonl"
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_max_queued_below_one_is_rejected(tmp_path: Path, config_module) -> None:
    text = (
        VALID_CONFIG
        + """\

[tasks]
max_queued = 0
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_task_timeout_defaults(config_module) -> None:
    tasks = config_module.TaskSettings(allowed_roots=(), denied_roots=())
    assert tasks.default_timeout_s == 14400
    assert tasks.idle_timeout_s == 600


def test_default_template_task_timeouts(default_config) -> None:
    assert default_config.tasks.default_timeout_s == 14400
    assert default_config.tasks.idle_timeout_s == 600


def test_idle_timeout_zero_is_accepted(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG + "\n[tasks]\nidle_timeout_s = 0\n"
    assert load_text(tmp_path, config_module, text).tasks.idle_timeout_s == 0


def test_idle_timeout_below_zero_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG + "\n[tasks]\nidle_timeout_s = -1\n"
    messages = error_messages(tmp_path, config_module, text)
    assert "tasks.idle_timeout_s: must be at least 0" in messages


@pytest.mark.parametrize("field", ["allowed_roots", "denied_roots"])
@pytest.mark.parametrize("root", ["", ".", "projects", "C:foo"])
def test_task_roots_must_be_absolute(tmp_path: Path, config_module, field: str, root: str) -> None:
    text = VALID_CONFIG + f"\n[tasks]\n{field} = [{json.dumps(root)}]\n"
    messages = error_messages(tmp_path, config_module, text)
    assert f"tasks.{field}[0]: must be an absolute path" in messages


@pytest.mark.parametrize("field", ["allowed_roots", "denied_roots"])
def test_absolute_task_root_is_accepted(tmp_path: Path, config_module, field: str) -> None:
    root = tmp_path / "work"
    text = VALID_CONFIG + f"\n[tasks]\n{field} = [{json.dumps(str(root))}]\n"
    config = load_text(tmp_path, config_module, text)
    assert getattr(config.tasks, field) == (root,)


@pytest.mark.parametrize("field", ["pool", "backend", "model"])
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
def test_harness_route_requires_fields(
    tmp_path: Path, config_module, field: str, value: str | None
) -> None:
    lines = [line for line in VALID_CONFIG.splitlines() if not line.startswith(f"{field} =")]
    text = "\n".join(lines) + "\n"
    if value is not None:
        text = text.replace('id = "route"', f'id = "route"\n{field} = ""')
    messages = error_messages(tmp_path, config_module, text)
    assert f"routes[0].{field}: a non-empty string is required" in messages


@pytest.mark.parametrize("backend", ["SQLite", "bad backend", "bad!"])
def test_invalid_storage_backend_is_rejected(tmp_path: Path, config_module, backend: str) -> None:
    text = (
        VALID_CONFIG
        + f"""\

[storage]
backend = {json.dumps(backend)}
"""
    )
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_config_errors_are_collected_together(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('unit = "percent"', 'unit = "percent"\nreserve = 2')
    text = text.replace('pool = "pool"', 'pool = "missing"')
    text += """\

[[sinks]]
path = "events.jsonl"
"""
    with pytest.raises(config_module.ConfigError) as caught:
        load_text(tmp_path, config_module, text)
    assert len(caught.value.messages) >= 3


def test_config_error_string_contains_all_messages(config_module) -> None:
    error = config_module.ConfigError(["first problem", "second problem"])
    assert "first problem" in str(error)
    assert "second problem" in str(error)


def test_anchor_is_converted_to_utc(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace(
        'role = "pace"', 'role = "pace"\nanchor = 2026-01-31T12:00:00+03:00'
    )
    config = load_text(tmp_path, config_module, text)
    assert config.pools[0].windows[0].anchor == datetime(2026, 1, 31, 9, tzinfo=UTC)


def test_naive_anchor_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('role = "pace"', 'role = "pace"\nanchor = 2026-01-31T12:00:00')
    with pytest.raises(config_module.ConfigError):
        load_text(tmp_path, config_module, text)


def test_config_uses_immutable_containers(default_config) -> None:
    assert isinstance(default_config.pools, tuple)
    assert isinstance(default_config.routes, tuple)
    assert isinstance(default_config.sinks, tuple)
    assert isinstance(default_config.pools[0].windows, tuple)
    assert isinstance(default_config.callers, MappingProxyType)
    assert isinstance(default_config.backends, MappingProxyType)
    assert isinstance(default_config.storage.options, MappingProxyType)


def test_config_dataclasses_are_frozen(default_config) -> None:
    with pytest.raises(FrozenInstanceError):
        default_config.port = 9000
    with pytest.raises(FrozenInstanceError):
        default_config.pools[0].id = "changed"
    with pytest.raises(FrozenInstanceError):
        default_config.tasks.max_queued = 1


def test_agent_router_home_overrides_home_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_module
) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(tmp_path / "custom-home"))
    assert config_module.home_dir() == tmp_path / "custom-home"


def test_tilde_in_home_and_roots_expands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_module
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AGENT_ROUTER_HOME", "~/ar")
    config = load_text(
        tmp_path,
        config_module,
        VALID_CONFIG + '\n[tasks]\nallowed_roots = ["~/work"]\ndenied_roots = ["~/private"]\n',
    )

    assert config_module.home_dir() == tmp_path / "ar"
    assert config.tasks.allowed_roots == (tmp_path / "work",)
    assert config.tasks.denied_roots == (tmp_path / "private",)


def test_unknown_route_key_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace("quality = 3", "quality = 3\nconfrm = true")
    messages = error_messages(tmp_path, config_module, text)
    assert any("routes[0].confrm: unknown key" in message for message in messages)


def test_unknown_top_level_section_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG + "\n[sever]\nport = 8787\n"
    messages = error_messages(tmp_path, config_module, text)
    assert any("sever: unknown key" in message for message in messages)


def test_unknown_nested_key_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('role = "pace"', 'role = "pace"\nreset_day = 1')
    text += '\n[classifier]\nkind = "none"\nconfidense = 0.9\n'
    messages = error_messages(tmp_path, config_module, text)
    assert any("pools[0].windows[0].reset_day: unknown key" in m for m in messages)
    assert any("classifier.confidense: unknown key" in m for m in messages)


def test_upstreams_not_supported(tmp_path: Path, config_module) -> None:
    text = (
        VALID_CONFIG
        + """
[upstreams.x]
kind = "openai"
pool = "pool"
base_url = "https://example.invalid/v1"
key_env = "X_KEY"
"""
    )
    messages = error_messages(tmp_path, config_module, text)
    assert any(
        "upstreams: API proxy is not implemented yet, remove the section" in message
        for message in messages
    )


def test_route_kind_api_not_supported(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace("quality = 3", 'kind = "api"\nquality = 3')
    messages = error_messages(tmp_path, config_module, text)
    assert any(
        "routes[0].kind: API proxy is not implemented yet" in message for message in messages
    )


def test_default_template_loads(tmp_path: Path, config_module) -> None:
    load_text(tmp_path, config_module, config_module.default_config_text())


def test_template_write_routes_need_confirm(config_module) -> None:
    raw = tomllib.loads(config_module.default_config_text(ALL_HARNESSES))
    for route in raw["routes"]:
        if "write" in route.get("modes", []):
            assert route.get("confirm") is True, route["id"]


def test_template_has_no_author_limits(config_module) -> None:
    text = config_module.default_config_text(ALL_HARNESSES)
    raw = tomllib.loads(text)
    limits = [
        window["limit_usd"]
        for pool in raw["pools"]
        for window in pool.get("windows", [])
        if "limit_usd" in window
    ]
    assert limits
    assert not ({12, 30, 60} & set(limits))
    for line in text.splitlines():
        if "limit_usd" in line:
            assert "# set your plan limit" in line


def test_template_packaged() -> None:
    root = importlib.resources.files("agent_router")
    assert (root / "config.example.toml").is_file()


def test_default_config_comments_all_routes(tmp_path: Path, config_module) -> None:
    config = load_text(tmp_path, config_module, config_module.default_config_text())
    assert config.routes == ()


def error_messages(tmp_path: Path, module, text: str) -> list[str]:
    with pytest.raises(module.ConfigError) as caught:
        load_text(tmp_path, module, text)
    return caught.value.messages


def test_several_errors_name_their_places(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG.replace('duration = "7d"', 'duration = "seven days"')
    text = text.replace('tiers = ["standard"]', 'tiers = ["unknown"]')
    messages = error_messages(tmp_path, config_module, text)
    assert len(messages) >= 2
    assert any("duration" in message for message in messages)
    assert any("tiers" in message for message in messages)


@pytest.mark.parametrize("line", ["duration = 5", ""])
def test_non_string_duration_is_a_config_error(tmp_path: Path, config_module, line: str) -> None:
    text = VALID_CONFIG.replace('duration = "7d"', line)
    messages = error_messages(tmp_path, config_module, text)
    assert any("duration" in message for message in messages)


@pytest.mark.parametrize("anchor", ['"2026-01-31T12:00:00+03:00"', "2026-01-31", "12:00:00", "5"])
def test_anchor_must_be_toml_datetime(tmp_path: Path, config_module, anchor: str) -> None:
    text = VALID_CONFIG.replace('role = "pace"', f'role = "pace"\nanchor = {anchor}')
    messages = error_messages(tmp_path, config_module, text)
    assert any("anchor" in message for message in messages)


def test_statusline_must_point_to_existing_pool_and_windows(tmp_path: Path, config_module) -> None:
    unknown_pool = VALID_CONFIG + '\n[statusline]\npool = "missing"\n'
    assert any(
        "statusline.pool" in message
        for message in error_messages(tmp_path, config_module, unknown_pool)
    )
    unknown_windows = VALID_CONFIG + '\n[statusline]\npool = "pool"\n'
    messages = error_messages(tmp_path, config_module, unknown_windows)
    assert any("statusline.five_hour" in message for message in messages)
    assert not any("statusline.seven_day" in message for message in messages)
    fine = VALID_CONFIG + '\n[statusline]\npool = "pool"\nfive_hour = "week"\n'
    assert load_text(tmp_path, config_module, fine).statusline.five_hour == "week"


def test_default_template_statusline_matches_claude_pool(default_config) -> None:
    assert default_config.statusline is not None
    assert default_config.statusline.pool == "claude-pro"
    assert (default_config.statusline.five_hour, default_config.statusline.seven_day) == (
        "5h",
        "week",
    )


@pytest.mark.parametrize(
    "line",
    ["port = 0", "port = 65536", "counter_interval_s = 0", "snapshot_ttl_s = -1.0"],
)
def test_server_values_out_of_range_are_rejected(tmp_path: Path, config_module, line: str) -> None:
    messages = error_messages(tmp_path, config_module, VALID_CONFIG + f"\n[server]\n{line}\n")
    assert any(line.split()[0] in message for message in messages)


def test_empty_agent_router_home_is_ignored(monkeypatch: pytest.MonkeyPatch, config_module) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", "")
    assert config_module.home_dir().name == "agent-router"
    assert config_module.home_dir().is_absolute()


def test_default_template_has_placeholder_limits(default_config) -> None:
    pools = {pool.id: pool for pool in default_config.pools}
    assert pools["claude-pro"].reserve == 0.15 and pools["claude-pro"].reserve_tier == "strong"
    assert [(w.id, w.kind, w.role) for w in pools["kimi-code"].windows] == [
        ("5h", "rolling", "guard"),
        ("month", "fixed", "pace"),
    ]
    go = pools["opencode-go"]
    assert go.unit == "usd" and go.counter == "opencode_stats"
    assert go.counter_provider == "opencode-go"
    assert [(w.id, w.kind, w.role, w.limit_usd) for w in go.windows] == [
        ("5h", "rolling", "guard", 10.0),
        ("week", "rolling", "guard", 10.0),
        ("month", "fixed", "pace", 10.0),
    ]
    assert [w.block_at for w in go.windows] == [0.9, 0.9, 1.0]
    routes = {route.id: route for route in default_config.routes}
    assert set(routes) == {
        "claude-cli/sonnet",
        "cline/glm-5.3",
        "kimi-cli/k3.1-pro",
        "kimi-cli/kimi-for-coding",
        "opencode-cli/glm-5.3",
        "opencode-cli/glm-5.3-flash",
    }
    assert routes["kimi-cli/k3.1-pro"].tiers == frozenset({"standard", "strong"})
    assert routes["kimi-cli/k3.1-pro"].model == "kimi-code/k3.1-pro"
    assert routes["opencode-cli/glm-5.3-flash"].tiers == frozenset({"cheap"})
    assert [routes[name].quality for name in sorted(routes)] == [5, 4, 4, 3, 4, 2]
    assert all(route.modes == frozenset({"read", "write"}) for route in routes.values())
    assert default_config.port == 8787 and default_config.classifier.kind == "none"
    assert default_config.tasks.allowed_roots == ()


def _classifier_text(body: str) -> str:
    return VALID_CONFIG + "\n[classifier]\n" + body


def _classifier_models_text(body: str) -> str:
    return VALID_CONFIG + "\n[[classifier.models]]\n" + body


def test_classifier_tev1_kind_is_rejected(tmp_path: Path, config_module) -> None:
    messages = error_messages(
        tmp_path, config_module, _classifier_text('kind = "tev1"\nmodel = "tev1:4b-q4_K_M"\n')
    )
    assert any("classifier.kind: invalid value" in message for message in messages)


@pytest.mark.parametrize("kind", ["systemone", "openai"])
def test_classifier_accepts_systemone_and_openai(tmp_path: Path, config_module, kind: str) -> None:
    text = _classifier_text(f'kind = "{kind}"\nmodel = "tev1:4b-q4_K_M"\n')
    config = load_text(tmp_path, config_module, text)
    assert config.classifier.kind == kind


def test_classifier_remote_https_with_key_env_accepted(tmp_path: Path, config_module) -> None:
    text = _classifier_models_text(
        'id = "remote"\n'
        'kind = "openai"\n'
        'url = "https://classifier.example.com/v1"\n'
        'model = "remote-model"\n'
        'api_key_env = "CLASSIFIER_API_KEY"\n'
    )
    config = load_text(tmp_path, config_module, text)
    model = config.classifier.effective_models[0]
    assert model.kind == "openai"
    assert model.api_key_env == "CLASSIFIER_API_KEY"


def test_classifier_remote_http_with_key_env_is_rejected(tmp_path: Path, config_module) -> None:
    text = _classifier_models_text(
        'id = "remote"\n'
        'kind = "openai"\n'
        'url = "http://classifier.example.com/v1"\n'
        'model = "remote-model"\n'
        'api_key_env = "CLASSIFIER_API_KEY"\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert (
        "classifier.models[0].url: address must be loopback, or https with api_key_env" in messages
    )


def test_classifier_remote_https_without_key_env_is_rejected(tmp_path: Path, config_module) -> None:
    text = _classifier_models_text(
        'id = "remote"\n'
        'kind = "openai"\n'
        'url = "https://classifier.example.com/v1"\n'
        'model = "remote-model"\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert (
        "classifier.models[0].url: address must be loopback, or https with api_key_env" in messages
    )


def test_classifier_bad_key_env_name_is_rejected(tmp_path: Path, config_module) -> None:
    text = _classifier_models_text(
        'id = "remote"\n'
        'kind = "openai"\n'
        'url = "https://classifier.example.com/v1"\n'
        'model = "remote-model"\n'
        'api_key_env = "1BAD-KEY"\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert "classifier.models[0].api_key_env: not a valid environment variable name" in messages


def test_classifier_loopback_with_key_env_accepted(tmp_path: Path, config_module) -> None:
    text = _classifier_models_text(
        'id = "local"\n'
        'kind = "systemone"\n'
        'url = "http://localhost:11434"\n'
        'model = "tev1:4b-q4_K_M"\n'
        'api_key_env = "LOCAL_KEY"\n'
    )
    config = load_text(tmp_path, config_module, text)
    assert config.classifier.effective_models[0].api_key_env == "LOCAL_KEY"


def test_classifier_single_model_key_env_propagates(tmp_path: Path, config_module) -> None:
    text = _classifier_text(
        'kind = "systemone"\nmodel = "tev1:4b-q4_K_M"\napi_key_env = "SINGLE_KEY"\n'
    )
    config = load_text(tmp_path, config_module, text)
    assert config.classifier.api_key_env == "SINGLE_KEY"
    assert config.classifier.effective_models[0].api_key_env == "SINGLE_KEY"


@pytest.mark.parametrize(
    "url", ["https://classifier.example:not-a-port/v1", "http://127.0.0.1:99999"]
)
def test_classifier_url_with_bad_port_is_rejected(tmp_path: Path, config_module, url: str) -> None:
    text = _classifier_models_text(
        'id = "remote"\n'
        'kind = "openai"\n'
        f'url = "{url}"\n'
        'model = "remote-model"\n'
        'api_key_env = "CLASSIFIER_API_KEY"\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert (
        "classifier.models[0].url: address must be loopback, or https with api_key_env" in messages
    )


def test_classifier_single_model_keeps_common_settings(tmp_path: Path, config_module) -> None:
    text = _classifier_text(
        'kind = "openai"\n'
        'url = "https://classifier.example.com/v1"\n'
        'model = "remote-model"\n'
        'api_key_env = "CLASSIFIER_API_KEY"\n'
        'mode = "active"\n'
        "min_confidence = 0.2\n"
        'write_floor = "strong"\n'
        "wait_s = 9\n"
        "vram_budget_mb = 1000\n"
    )
    settings = load_text(tmp_path, config_module, text).classifier
    assert settings.mode == "active"
    assert settings.min_confidence == 0.2
    assert settings.write_floor == "strong"
    assert settings.wait_s == 9
    assert settings.vram_budget_mb == 1000


def test_classifier_single_model_common_settings_are_validated(
    tmp_path: Path, config_module
) -> None:
    text = _classifier_text('kind = "systemone"\nmin_confidence = 2\nwait_s = -1\n')
    messages = error_messages(tmp_path, config_module, text)
    assert "classifier.min_confidence: must be in the range 0..1" in messages
    assert "classifier.wait_s: cannot be negative" in messages


def test_classifier_key_env_with_models_is_rejected(tmp_path: Path, config_module) -> None:
    text = VALID_CONFIG + (
        '\n[classifier]\napi_key_env = "LOCAL_API_KEY"\n'
        '\n[[classifier.models]]\nid = "studio"\nkind = "openai"\n'
        'url = "http://127.0.0.1:1234/v1"\nmodel = "model"\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert "classifier: old form and [[classifier.models]] used together" in messages


def test_init_template_has_routing_keys(default_config) -> None:
    assert default_config.models["kimi-k3.1-pro"].prefer == ("kimi-code", "opencode-go")
    routes = {route.id: route for route in default_config.routes}
    cline = routes["cline/glm-5.3"]
    assert (cline.enabled, cline.backend, cline.pool) == (False, "cline", "opencode-go")
    assert cline.model == "opencode-go/glm-5.3" and cline.model_key == "glm-5.3"
    assert cline.tiers == frozenset({"standard"}) and cline.quality == 4
    assert routes["kimi-cli/k3.1-pro"].model_key == "kimi-k3.1-pro"
    assert default_config.callers["cline"] == "opencode-go"
    assert default_config.callers["opencode"] == "opencode-go"
    assert default_config.callers["claude-code"] == "claude-pro"
    assert default_config.route_spend(cline) == "journal"


def _usage_text(root: str = "", pool: str = "") -> str:
    text = VALID_CONFIG
    if pool:
        text = text.replace("[[routes]]", "[pools.usage]\n" + pool + "\n[[routes]]")
    if root:
        text += "\n[usage]\n" + root
    return text


@pytest.fixture
def fake_usage_source(monkeypatch: pytest.MonkeyPatch):
    """A registered fake usage source so pool tables with source pass registry validation."""
    from agent_router import registry
    from agent_router.usage import UsageSourceSpec

    spec = UsageSourceSpec(
        "fake-usage",
        lambda pool_usage, env: None,
        frozenset({"monthly", "rolling"}),
        "fake_usage",
    )
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)
    return spec


def test_usage_defaults(tmp_path: Path, config_module) -> None:
    config = load_text(tmp_path, config_module, VALID_CONFIG)
    assert (config.usage.mode, config.usage.interval_s) == ("auto", 3600)
    assert config.pools[0].usage == config_module.PoolUsage()
    assert config.usage_mode("pool") == "auto"


def test_usage_root_table_parses(tmp_path: Path, config_module) -> None:
    root = 'mode = "manual"\ninterval = "30m"\n'
    config = load_text(tmp_path, config_module, _usage_text(root=root))
    assert (config.usage.mode, config.usage.interval_s) == ("manual", 1800)


@pytest.mark.parametrize("root", ['mode = "nope"\n', 'interval = "abc"\n', "interval = 5\n"])
def test_usage_root_bad_values_are_rejected(tmp_path: Path, config_module, root: str) -> None:
    messages = error_messages(tmp_path, config_module, _usage_text(root=root))
    key = root.split()[0]
    assert any(message.startswith(f"usage.{key}:") for message in messages)


def test_usage_root_unknown_key_is_rejected(tmp_path: Path, config_module) -> None:
    messages = error_messages(tmp_path, config_module, _usage_text(root="every = 5\n"))
    assert "usage.every: unknown key" in messages


def test_pool_usage_maps_windows(tmp_path: Path, config_module, fake_usage_source) -> None:
    text = _usage_text(pool='source = "fake-usage"\nwindows = { monthly = "week" }\n')
    usage = load_text(tmp_path, config_module, text).pools[0].usage
    assert usage.source == "fake-usage"
    assert dict(usage.windows) == {"monthly": "week"}


def test_pool_usage_unknown_window_is_rejected(
    tmp_path: Path, config_module, fake_usage_source
) -> None:
    text = _usage_text(pool='source = "fake-usage"\nwindows = { monthly = "nope" }\n')
    messages = error_messages(tmp_path, config_module, text)
    assert 'pools[0].usage.windows.monthly: unknown window "nope"' in messages


def test_pool_usage_two_fields_one_window_is_rejected(
    tmp_path: Path, config_module, fake_usage_source
) -> None:
    text = _usage_text(
        pool='source = "fake-usage"\nwindows = { monthly = "week", rolling = "week" }\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert (
        'pools[0].usage.windows.rolling: window "week" is already mapped from field "monthly"'
        in messages
    )


def test_pool_usage_source_without_windows_is_rejected(
    tmp_path: Path, config_module, fake_usage_source
) -> None:
    text = _usage_text(pool='source = "fake-usage"\n')
    messages = error_messages(tmp_path, config_module, text)
    assert "pools[0].usage.windows: map at least one source field to a window" in messages


@pytest.mark.parametrize("pool", ['windows = { monthly = "week" }\n', 'api_key_env = "X_KEY"\n'])
def test_pool_usage_without_source_is_rejected(tmp_path: Path, config_module, pool: str) -> None:
    messages = error_messages(tmp_path, config_module, _usage_text(pool=pool))
    assert "pools[0].usage: windows and api_key_env require source" in messages


def test_pool_usage_executable_is_read(tmp_path: Path, config_module, fake_usage_source) -> None:
    text = _usage_text(
        pool='source = "fake-usage"\nexecutable = "C:/tools/claude.exe"\n'
        'windows = { monthly = "week" }\n'
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    config = config_module.load_config(path)
    assert config.pools[0].usage.executable == "C:/tools/claude.exe"


def test_pool_usage_executable_without_source_is_rejected(tmp_path: Path, config_module) -> None:
    text = _usage_text(pool='executable = "claude"\n')
    messages = error_messages(tmp_path, config_module, text)
    assert "pools[0].usage: executable requires source" in messages


def test_pool_usage_empty_executable_is_rejected(
    tmp_path: Path, config_module, fake_usage_source
) -> None:
    text = _usage_text(
        pool='source = "fake-usage"\nexecutable = " "\nwindows = { monthly = "week" }\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert "pools[0].usage.executable: must not be empty" in messages


def test_pool_usage_bad_api_key_env_is_rejected(
    tmp_path: Path, config_module, fake_usage_source
) -> None:
    text = _usage_text(
        pool='source = "fake-usage"\napi_key_env = "1BAD"\nwindows = { monthly = "week" }\n'
    )
    messages = error_messages(tmp_path, config_module, text)
    assert "pools[0].usage.api_key_env: must look like an environment variable name" in messages


def test_pool_usage_bad_mode_is_rejected(tmp_path: Path, config_module) -> None:
    text = _usage_text(pool='mode = "nope"\n')
    messages = error_messages(tmp_path, config_module, text)
    assert "pools[0].usage.mode: invalid value: 'nope'; allowed: auto, manual" in messages


def test_pool_usage_unknown_key_is_rejected(tmp_path: Path, config_module) -> None:
    messages = error_messages(tmp_path, config_module, _usage_text(pool="src = 5\n"))
    assert "pools[0].usage.src: unknown key" in messages


def test_usage_mode_inheritance(tmp_path: Path, config_module, fake_usage_source) -> None:
    text = _usage_text(
        root='mode = "manual"\n',
        pool='source = "fake-usage"\nmode = "auto"\nwindows = { monthly = "week" }\n',
    )
    config = load_text(tmp_path, config_module, text)
    assert config.usage_mode("pool") == "auto"  # pool override wins
    assert config.usage_mode("missing") == "manual"  # anything else inherits the root


def test_auto_mode_without_source_is_valid(tmp_path: Path, config_module) -> None:
    config = load_text(tmp_path, config_module, _usage_text(root='mode = "auto"\n'))
    assert config.usage.mode == "auto"


def test_default_template_usage_is_commented_out(default_config) -> None:
    assert (default_config.usage.mode, default_config.usage.interval_s) == ("auto", 3600)
    assert all(pool.usage == PoolUsage() for pool in default_config.pools)
