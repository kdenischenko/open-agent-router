"""TOML config parsing, validation, template, and loading."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Collection, Mapping
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import platformdirs

from agent_router.clock import to_utc
from agent_router.config_classifier import (
    _parse_classifier,
)
from agent_router.config_model import (
    TIERS,
    BackendSection,
    ClassifierModel,
    ClassifierSettings,
    Config,
    ConfigError,
    Mode,
    ModelSettings,
    Pool,
    PoolUsage,
    Route,
    RouteKind,
    SinkSettings,
    SpendSource,
    StatuslineSettings,
    StorageSettings,
    TaskSettings,
    Tier,
    UpstreamSettings,
    UsageSettings,
    Window,
    effort_of,
    model_key,
)
from agent_router.config_values import (
    _boolean,
    _check_cmd_text,
    _check_keys,
    _choice,
    _integer,
    _number,
    _optional_text,
    _paths,
    _required_text,
    _strings,
    _table,
    _tables,
    _text,
)
from agent_router.periods import Duration, parse_duration, parse_span
from agent_router.prices import PriceTable, load_prices

# the config model lives in config_model; callers keep importing it from here
__all__ = [
    "BackendSection",
    "ClassifierModel",
    "ClassifierSettings",
    "Config",
    "ConfigError",
    "default_config_text",
    "effort_of",
    "home_dir",
    "load_config",
    "Mode",
    "model_key",
    "ModelSettings",
    "Pool",
    "PoolUsage",
    "Route",
    "RouteKind",
    "SinkSettings",
    "SpendSource",
    "StatuslineSettings",
    "StorageSettings",
    "TaskSettings",
    "Tier",
    "TIERS",
    "UpstreamSettings",
    "UsageSettings",
    "Window",
]


_STORAGE_BACKEND = re.compile(r"[a-z0-9_-]+")
_DENIED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
    }
)


_WINDOW_KEYS = frozenset({"id", "duration", "kind", "role", "anchor", "limit_usd", "block_at"})


def _parse_windows(value: Any, unit: str, pool_label: str, errors: list[str]) -> tuple[Window, ...]:
    windows: list[Window] = []
    seen: set[str] = set()
    for index, raw in enumerate(_tables(value, f"{pool_label}.windows", errors)):
        label = f"{pool_label}.windows[{index}]"
        _check_keys(raw, _WINDOW_KEYS, label, errors)
        window_id = _text(raw.get("id"), f"{label}.id", errors)
        if window_id in seen:
            errors.append(f"{label}.id: duplicate id: {window_id!r}")
        seen.add(window_id)
        duration = Duration(hours=1)
        if not isinstance(raw.get("duration"), str):
            errors.append(f"{label}.duration: a string like 5h, 7d, 1mo is required")
        else:
            try:
                duration = parse_duration(raw["duration"])
            except ValueError as exc:
                errors.append(f"{label}.duration: {exc}")
        kind = _choice(raw.get("kind"), ("fixed", "rolling"), f"{label}.kind", errors, "fixed")
        role = _choice(raw.get("role"), ("pace", "guard"), f"{label}.role", errors, "guard")
        if kind == "rolling" and role == "pace":
            errors.append(f"{label}: a rolling window cannot have the pace role")
        anchor = None
        if raw.get("anchor") is not None:
            if not isinstance(raw["anchor"], datetime):
                errors.append(f"{label}.anchor: a TOML date-time with an offset is required")
            else:
                try:
                    anchor = to_utc(raw["anchor"])
                except ValueError as exc:
                    errors.append(f"{label}.anchor: {exc}")
        limit_usd = raw.get("limit_usd")
        if unit == "usd" and limit_usd is None:
            errors.append(f"{label}.limit_usd: required for a usd window")
        if limit_usd is not None:
            limit_usd = _number(limit_usd, f"{label}.limit_usd", errors, 0.0)
            if limit_usd <= 0:
                errors.append(f"{label}.limit_usd: must be positive")
        block_at = _number(
            raw.get("block_at", 0.9 if role == "guard" else 1.0),
            f"{label}.block_at",
            errors,
            1.0,
        )
        if not 0 <= block_at <= 1:
            errors.append(f"{label}.block_at: must be in the range 0..1")
        windows.append(
            Window(
                id=window_id,
                duration=duration,
                kind=kind,
                role=role,
                anchor=anchor,
                limit_usd=limit_usd,
                block_at=block_at,
            )
        )
    return tuple(windows)


def _parse_levers(
    raw: dict[str, Any], label: str, errors: list[str]
) -> tuple[int | None, float | None]:
    priority = raw.get("priority")
    if priority is not None:
        priority = _integer(priority, f"{label}.priority", errors, 0)
    weight = raw.get("weight")
    if weight is not None:
        weight = _number(weight, f"{label}.weight", errors, 0.0)
    return priority, weight


_POOL_KEYS = frozenset(
    {
        "id",
        "title",
        "unit",
        "windows",
        "reserve",
        "reserve_tier",
        "counter",
        "counter_provider",
        "counter_model",
        "priority",
        "weight",
        "price_file",
        "limit_base_usd",
        "model_limits",
        "model_limits_date",
        "usage",
    }
)


_POOL_USAGE_KEYS = frozenset({"source", "mode", "api_key_env", "windows"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_pool_usage(value: Any, label: str, window_ids: set[str], errors: list[str]) -> PoolUsage:
    if value is None:
        return PoolUsage()
    raw = _table(value, f"{label}.usage", errors)
    _check_keys(raw, _POOL_USAGE_KEYS, f"{label}.usage", errors)
    source = _optional_text(raw.get("source"), f"{label}.usage.source", errors)
    if source is not None and not source.strip():
        errors.append(f"{label}.usage.source: must not be empty")
        source = None
    mode = _optional_text(raw.get("mode"), f"{label}.usage.mode", errors)
    if mode is not None and mode not in ("auto", "manual"):
        errors.append(f"{label}.usage.mode: invalid value: {mode!r}; allowed: auto, manual")
        mode = None
    api_key_env = _optional_text(raw.get("api_key_env"), f"{label}.usage.api_key_env", errors)
    if api_key_env is not None and _ENV_NAME.fullmatch(api_key_env) is None:
        errors.append(f"{label}.usage.api_key_env: must look like an environment variable name")
        api_key_env = None
    windows: dict[str, str] = {}
    mapped: dict[str, str] = {}  # window id -> first field mapped to it
    for key, item in _table(raw.get("windows"), f"{label}.usage.windows", errors).items():
        if not isinstance(item, str):
            errors.append(f"{label}.usage.windows.{key}: expected a window id string")
            continue
        if item not in window_ids:
            errors.append(f'{label}.usage.windows.{key}: unknown window "{item}"')
            continue
        if item in mapped:
            reason = f'window "{item}" is already mapped from field "{mapped[item]}"'
            errors.append(f"{label}.usage.windows.{key}: {reason}")
            continue
        mapped[item] = key
        windows[key] = item
    if source is None and (windows or api_key_env is not None):
        errors.append(f"{label}.usage: windows and api_key_env require source")
    if source is not None and not windows:
        errors.append(f"{label}.usage.windows: map at least one source field to a window")
    return PoolUsage(source=source, mode=mode, api_key_env=api_key_env, windows=windows)


def _parse_pools(value: Any, directory: Path, errors: list[str]) -> tuple[Pool, ...]:
    pools: list[Pool] = []
    seen: set[str] = set()
    for index, raw in enumerate(_tables(value, "pools", errors)):
        label = f"pools[{index}]"
        _check_keys(raw, _POOL_KEYS, label, errors)
        pool_id = _text(raw.get("id"), f"{label}.id", errors)
        if pool_id in seen:
            errors.append(f"{label}.id: duplicate id: {pool_id!r}")
        seen.add(pool_id)
        title = _text(raw.get("title"), f"{label}.title", errors)
        unit = _choice(raw.get("unit"), ("percent", "usd"), f"{label}.unit", errors, "percent")
        windows = _parse_windows(raw.get("windows"), unit, label, errors)
        usage = _parse_pool_usage(
            raw.get("usage"), label, {window.id for window in windows}, errors
        )
        if not any(window.role == "pace" for window in windows):
            errors.append(f"{label}: the pool must have at least one pace window")
        reserve = _number(raw.get("reserve", 0.0), f"{label}.reserve", errors, 0.0)
        if not 0 <= reserve <= 1:
            errors.append(f"{label}.reserve: must be in the range 0..1")
        reserve_tier = _optional_text(raw.get("reserve_tier"), f"{label}.reserve_tier", errors)
        if reserve_tier is not None and reserve_tier not in TIERS:
            errors.append(f"{label}.reserve_tier: unknown tier: {reserve_tier!r}")
        priority, weight = _parse_levers(raw, label, errors)
        raw_price_file = _optional_text(raw.get("price_file"), f"{label}.price_file", errors)
        price_file = None
        prices: PriceTable | None = None
        if raw_price_file is not None:
            price_file = Path(raw_price_file).expanduser()
            if not price_file.is_absolute():
                price_file = directory / price_file
            try:
                prices = load_prices(price_file)
            except (OSError, ValueError) as exc:
                errors.append(f"{label}.price_file: {exc}")
        limit_base_usd: float | None = None
        raw_base = raw.get("limit_base_usd")
        if raw_base is not None:
            base = _number(raw_base, f"{label}.limit_base_usd", errors, 1.0)
            if base > 0:
                limit_base_usd = base
            else:
                errors.append(f"{label}.limit_base_usd: must be positive")
        model_limits: dict[str, float] = {}
        for key, value in _table(raw.get("model_limits"), f"{label}.model_limits", errors).items():
            if not isinstance(key, str):
                errors.append(f"{label}.model_limits: key {key!r} must be a string")
                continue
            limit = _number(value, f"{label}.model_limits.{key}", errors, 0.0)
            if limit <= 0:
                errors.append(f"{label}.model_limits: value for {key!r} must be positive")
            else:
                model_limits[key] = limit
        if model_limits and limit_base_usd is None:
            errors.append(f"{label}.model_limits: limit_base_usd is required")
        model_limits_date: str | None = None
        raw_date = raw.get("model_limits_date")
        if raw_date is not None:
            if isinstance(raw_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                try:
                    model_limits_date = date.fromisoformat(raw_date).isoformat()
                except ValueError:
                    errors.append(f"{label}.model_limits_date: must be a date of form YYYY-MM-DD")
            elif isinstance(raw_date, date) and not isinstance(raw_date, datetime):
                model_limits_date = raw_date.isoformat()
            else:
                errors.append(f"{label}.model_limits_date: must be a date of form YYYY-MM-DD")
        pools.append(
            Pool(
                id=pool_id,
                title=title,
                unit=unit,
                windows=windows,
                reserve=reserve,
                reserve_tier=reserve_tier,
                counter=_optional_text(raw.get("counter"), f"{label}.counter", errors),
                counter_provider=_optional_text(
                    raw.get("counter_provider"), f"{label}.counter_provider", errors
                ),
                counter_model=_optional_text(
                    raw.get("counter_model"), f"{label}.counter_model", errors
                ),
                priority=priority,
                weight=weight,
                price_file=price_file,
                prices=prices,
                limit_base_usd=limit_base_usd,
                model_limits=model_limits,
                model_limits_date=model_limits_date,
                usage=usage,
            )
        )
    return tuple(pools)


_ROUTE_KEYS = frozenset(
    {
        "id",
        "pool",
        "kind",
        "tiers",
        "modes",
        "quality",
        "backend",
        "upstream",
        "model",
        "model_key",
        "priority",
        "weight",
        "spend",
        "timeout_s",
        "confirm",
        "enabled",
    }
)


def _parse_routes(
    value: Any, pool_ids: set[str], upstream_ids: set[str], errors: list[str]
) -> tuple[Route, ...]:
    routes: list[Route] = []
    seen: set[str] = set()
    for index, raw in enumerate(_tables(value, "routes", errors)):
        label = f"routes[{index}]"
        _check_keys(raw, _ROUTE_KEYS, label, errors)
        route_id = _text(raw.get("id"), f"{label}.id", errors)
        if route_id in seen:
            errors.append(f"{label}.id: duplicate id: {route_id!r}")
        seen.add(route_id)
        pool = _text(raw.get("pool"), f"{label}.pool", errors)
        if pool not in pool_ids:
            errors.append(f"{label}.pool: unknown pool: {pool!r}")
        raw_kind = raw.get("kind", "harness")
        if raw_kind not in ("harness", "api"):
            errors.append(f"{label}.kind: expected harness or api")
            raw_kind = "harness"
        kind: RouteKind = raw_kind
        if kind == "api":
            errors.append(f"{label}.kind: API proxy is not implemented yet")
        raw_tiers = _strings(raw.get("tiers", ["standard"]), f"{label}.tiers", errors)
        if raw.get("tiers") == []:
            errors.append(f"{label}.tiers: must not be empty")
        for tier in raw_tiers:
            if tier not in TIERS:
                errors.append(f"{label}.tiers: unknown tier: {tier!r}")
        raw_modes: list[str] = []
        if kind == "api":
            if "modes" in raw:
                errors.append(f"{label}: an api route has no modes")
        else:
            raw_modes = _strings(raw.get("modes", ["read", "write"]), f"{label}.modes", errors)
            if raw.get("modes") == []:
                errors.append(f"{label}.modes: must not be empty")
            for mode in raw_modes:
                if mode not in ("read", "write"):
                    errors.append(f"{label}.modes: unknown mode: {mode!r}")
        quality = _integer(raw.get("quality", 3), f"{label}.quality", errors, 3)
        if not 1 <= quality <= 5:
            errors.append(f"{label}.quality: must be in the range 1..5")
        backend = _optional_text(raw.get("backend"), f"{label}.backend", errors)
        upstream = _optional_text(raw.get("upstream"), f"{label}.upstream", errors)
        if kind == "harness":
            if upstream is not None:
                errors.append(f"{label}: a harness route has no upstream")
        else:
            if backend is not None:
                errors.append(f"{label}: an api route has no backend")
            if upstream is None:
                errors.append(f"{label}.upstream: required for an api route")
            elif upstream not in upstream_ids:
                errors.append(f"{label}.upstream: unknown upstream: {upstream!r}")
        model = _optional_text(raw.get("model"), f"{label}.model", errors)
        _check_cmd_text(model, f"{label}.model", errors)
        model_key = _optional_text(raw.get("model_key"), f"{label}.model_key", errors)
        priority, weight = _parse_levers(raw, label, errors)
        raw_spend = raw.get("spend")
        spend = None
        if raw_spend is not None:
            if raw_spend not in ("counter", "journal", "proxy"):
                errors.append(f"{label}.spend: expected counter, journal, or proxy")
            else:
                spend = raw_spend
        if kind == "api" and spend is not None and spend != "proxy":
            errors.append(f"{label}.spend: an api route only supports proxy")
        timeout_s = raw.get("timeout_s")
        if timeout_s is not None:
            timeout_s = _integer(timeout_s, f"{label}.timeout_s", errors, 0)
            if timeout_s < 1:
                errors.append(f"{label}.timeout_s: must be positive")
        routes.append(
            Route(
                id=route_id,
                pool=pool,
                tiers=frozenset(tier for tier in raw_tiers if tier in TIERS),
                quality=quality,
                backend=backend,
                model=model,
                modes=frozenset(mode for mode in raw_modes if mode in ("read", "write")),
                confirm=_boolean(raw.get("confirm", False), f"{label}.confirm", errors, False),
                enabled=_boolean(raw.get("enabled", True), f"{label}.enabled", errors, True),
                timeout_s=timeout_s,
                kind=kind,
                upstream=upstream,
                model_key=model_key,
                priority=priority,
                weight=weight,
                spend=spend,
            )
        )
    return tuple(routes)


def _parse_callers(value: Any, pool_ids: set[str], errors: list[str]) -> Mapping[str, str | None]:
    raw = _table(value, "callers", errors)
    callers: dict[str, str | None] = {}
    for caller, pool in raw.items():
        if not isinstance(pool, str):
            errors.append(f"callers.{caller}: expected a string")
            continue
        if pool and pool not in pool_ids:
            errors.append(f"callers.{caller}: unknown pool: {pool!r}")
        callers[caller] = pool or None
    return callers


_BACKEND_KEYS = frozenset({"harness", "executable", "priority", "weight"})


def _parse_backends(value: Any, errors: list[str]) -> Mapping[str, BackendSection]:
    raw = _table(value, "backends", errors)
    backends: dict[str, BackendSection] = {}
    for name, settings in raw.items():
        label = f"backends.{name}"
        table = _table(settings, label, errors)
        _check_keys(table, _BACKEND_KEYS, label, errors)
        harness = _optional_text(table.get("harness", name), f"{label}.harness", errors)
        executable = _optional_text(table.get("executable", ""), f"{label}.executable", errors)
        _check_cmd_text(executable, f"{label}.executable", errors)
        priority, weight = _parse_levers(table, label, errors)
        backends[name] = BackendSection(
            name, harness or name, executable or "", priority=priority, weight=weight
        )
    return backends


_MODEL_KEYS = frozenset({"prefer"})


def _parse_models(value: Any, pool_ids: set[str], errors: list[str]) -> Mapping[str, ModelSettings]:
    raw = _table(value, "models", errors)
    models: dict[str, ModelSettings] = {}
    for name, settings in raw.items():
        label = f'models."{name}"'
        table = _table(settings, label, errors)
        _check_keys(table, _MODEL_KEYS, label, errors)
        prefer = _strings(table.get("prefer", []), f"{label}.prefer", errors)
        seen: set[str] = set()
        for pool in prefer:
            if pool not in pool_ids:
                errors.append(f"{label}.prefer: unknown pool {pool}")
            if pool in seen:
                errors.append(f"{label}.prefer: pool {pool} is repeated")
            seen.add(pool)
        models[name] = ModelSettings(prefer=tuple(prefer))
    return models


_UPSTREAM_KEYS = frozenset(
    {
        "kind",
        "pool",
        "base_url",
        "key_file",
        "key_env",
        "pass_headers",
        "stream_usage",
        "enabled",
        "priority",
        "weight",
    }
)


def _parse_upstreams(
    value: Any, pool_ids: set[str], errors: list[str]
) -> Mapping[str, UpstreamSettings]:
    raw = _table(value, "upstreams", errors)
    upstreams: dict[str, UpstreamSettings] = {}
    for name, settings in raw.items():
        label = f"upstreams.{name}"
        table = _table(settings, label, errors)
        _check_keys(table, _UPSTREAM_KEYS, label, errors)
        kind = _required_text(table.get("kind"), f"{label}.kind", errors)
        pool = _required_text(table.get("pool"), f"{label}.pool", errors)
        if pool and pool not in pool_ids:
            errors.append(f"{label}.pool: unknown pool: {pool!r}")
        base_url = _required_text(table.get("base_url"), f"{label}.base_url", errors)
        key_file = _optional_text(table.get("key_file"), f"{label}.key_file", errors)
        key_env = _optional_text(table.get("key_env"), f"{label}.key_env", errors)
        if (key_file is None) == (key_env is None):
            errors.append(f"{label}: exactly one of key_file and key_env is required")
        pass_headers = _strings(table.get("pass_headers", []), f"{label}.pass_headers", errors)
        for header in pass_headers:
            if header.lower() in _DENIED_HEADERS:
                errors.append(f"{label}.pass_headers: header {header.lower()} must not be passed")
        priority, weight = _parse_levers(table, label, errors)
        upstreams[name] = UpstreamSettings(
            name=name,
            kind=kind,
            pool=pool,
            base_url=base_url,
            key_file=key_file,
            key_env=key_env,
            pass_headers=tuple(pass_headers),
            stream_usage=_boolean(
                table.get("stream_usage", True), f"{label}.stream_usage", errors, True
            ),
            enabled=_boolean(table.get("enabled", True), f"{label}.enabled", errors, True),
            priority=priority,
            weight=weight,
        )
    if upstreams:
        errors.append("upstreams: API proxy is not implemented yet, remove the section")
    return upstreams


_TASKS_KEYS = frozenset(
    {
        "allowed_roots",
        "denied_roots",
        "max_parallel",
        "max_queued",
        "max_attempts",
        "default_timeout_s",
        "idle_timeout_s",
        "approval_timeout_s",
        "limit_backoff_s",
        "limit_backoff_max_s",
        "max_prompt_chars",
        "exit_grace_s",
    }
)


def _parse_tasks(value: Any, errors: list[str]) -> TaskSettings:
    raw = _table(value, "tasks", errors)
    _check_keys(raw, _TASKS_KEYS, "tasks", errors)
    defaults = TaskSettings(allowed_roots=(), denied_roots=())
    values: dict[str, Any] = {
        "allowed_roots": _paths(raw.get("allowed_roots", []), "tasks.allowed_roots", errors),
        "denied_roots": _paths(raw.get("denied_roots", []), "tasks.denied_roots", errors),
    }
    for name in (
        "max_parallel",
        "max_queued",
        "max_attempts",
        "default_timeout_s",
        "idle_timeout_s",
        "approval_timeout_s",
        "limit_backoff_s",
        "limit_backoff_max_s",
        "max_prompt_chars",
        "exit_grace_s",
    ):
        default = getattr(defaults, name)
        values[name] = _integer(raw.get(name, default), f"tasks.{name}", errors, default)
        # 0 disables the idle check; every other task limit must be at least 1
        minimum = 0 if name == "idle_timeout_s" else 1
        if values[name] < minimum:
            errors.append(f"tasks.{name}: must be at least {minimum}")
    return TaskSettings(**values)


_STATUSLINE_KEYS = frozenset({"pool", "five_hour", "seven_day"})


def _parse_statusline(
    value: Any, pools: tuple[Pool, ...], errors: list[str]
) -> StatuslineSettings | None:
    if value is None:
        return None
    raw = _table(value, "statusline", errors)
    _check_keys(raw, _STATUSLINE_KEYS, "statusline", errors)
    settings = StatuslineSettings(
        pool=_text(raw.get("pool"), "statusline.pool", errors),
        five_hour=_text(raw.get("five_hour", "5h"), "statusline.five_hour", errors),
        seven_day=_text(raw.get("seven_day", "week"), "statusline.seven_day", errors),
    )
    pool = next((item for item in pools if item.id == settings.pool), None)
    if pool is None:
        if settings.pool:
            errors.append(f"statusline.pool: unknown pool: {settings.pool!r}")
        return settings
    window_ids = {window.id for window in pool.windows}
    for key, window_id in (("five_hour", settings.five_hour), ("seven_day", settings.seven_day)):
        if window_id and window_id not in window_ids:
            errors.append(f"statusline.{key}: pool {pool.id!r} has no window: {window_id!r}")
    return settings


def _parse_storage(value: Any, errors: list[str]) -> StorageSettings:
    raw = _table(value, "storage", errors)
    backend = _text(raw.get("backend", "sqlite"), "storage.backend", errors, "sqlite")
    if _STORAGE_BACKEND.fullmatch(backend) is None:
        errors.append("storage.backend: only [a-z0-9_-]+ characters are allowed")
    retention_days = None
    if raw.get("retention_days") is not None:
        retention_days = _integer(raw["retention_days"], "storage.retention_days", errors, 1)
        if retention_days < 1:
            errors.append("storage.retention_days: must be at least 1")
    return StorageSettings(
        backend=backend,
        retention_days=retention_days,
        options={
            key: item for key, item in raw.items() if key not in {"backend", "retention_days"}
        },
    )


def _parse_sinks(value: Any, errors: list[str]) -> tuple[SinkSettings, ...]:
    sinks: list[SinkSettings] = []
    for index, raw in enumerate(_tables(value, "sinks", errors)):
        label = f"sinks[{index}]"
        sink_type = _text(raw.get("type"), f"{label}.type", errors)
        sinks.append(
            SinkSettings(
                type=sink_type,
                options={key: item for key, item in raw.items() if key != "type"},
            )
        )
    return tuple(sinks)


_ROOT_KEYS = frozenset(
    {
        "pools",
        "routes",
        "callers",
        "tasks",
        "backends",
        "models",
        "statusline",
        "classifier",
        "storage",
        "sinks",
        "server",
        "upstreams",
        "usage",
    }
)
_SERVER_KEYS = frozenset({"port", "counter_interval_s", "snapshot_ttl_s", "panel_session_days"})

_USAGE_KEYS = frozenset({"mode", "interval"})
_MIN_USAGE_INTERVAL = timedelta(minutes=1)


def _parse_usage(value: Any, errors: list[str]) -> UsageSettings:
    raw = _table(value, "usage", errors)
    _check_keys(raw, _USAGE_KEYS, "usage", errors)
    mode = _choice(raw.get("mode", "auto"), ("auto", "manual"), "usage.mode", errors, "auto")
    interval_s = 3600
    if raw.get("interval") is not None:
        if not isinstance(raw["interval"], str):
            errors.append("usage.interval: a span string like 1h or 30m is required")
        else:
            try:
                span = parse_span(raw["interval"])
            except ValueError as exc:
                errors.append(f"usage.interval: {exc}")
            else:
                if span < _MIN_USAGE_INTERVAL:
                    errors.append("usage.interval: must be at least 1m")
                else:
                    interval_s = int(span.total_seconds())
    return UsageSettings(mode=mode, interval_s=interval_s, present=value is not None)


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        raw = _table(raw, "config", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError([f"{path}: {exc}"]) from exc

    errors: list[str] = []
    _check_keys(raw, _ROOT_KEYS, "config", errors)
    pools = _parse_pools(raw.get("pools"), path.parent, errors)
    pool_ids = {pool.id for pool in pools}
    upstreams = _parse_upstreams(raw.get("upstreams"), pool_ids, errors)
    routes = _parse_routes(raw.get("routes"), pool_ids, set(upstreams), errors)
    callers = _parse_callers(raw.get("callers"), pool_ids, errors)
    tasks = _parse_tasks(raw.get("tasks"), errors)
    backends = _parse_backends(raw.get("backends"), errors)
    models = _parse_models(raw.get("models"), pool_ids, errors)
    statusline = _parse_statusline(raw.get("statusline"), pools, errors)
    classifier = _parse_classifier(raw.get("classifier"), errors)
    storage = _parse_storage(raw.get("storage"), errors)
    usage = _parse_usage(raw.get("usage"), errors)
    sinks = _parse_sinks(raw.get("sinks"), errors)
    server = _table(raw.get("server"), "server", errors)
    _check_keys(server, _SERVER_KEYS, "server", errors)
    port = _integer(server.get("port", 8787), "server.port", errors, 8787)
    counter_interval_s = _integer(
        server.get("counter_interval_s", 300), "server.counter_interval_s", errors, 300
    )
    snapshot_ttl_s = _number(
        server.get("snapshot_ttl_s", 1.0), "server.snapshot_ttl_s", errors, 1.0
    )
    panel_session_days = _integer(
        server.get("panel_session_days", 30), "server.panel_session_days", errors, 30
    )
    if not 1 <= port <= 65535:
        errors.append("server.port: must be in the range 1..65535")
    if counter_interval_s < 1:
        errors.append("server.counter_interval_s: must be at least 1")
    if snapshot_ttl_s < 0:
        errors.append("server.snapshot_ttl_s: cannot be negative")
    if not 1 <= panel_session_days <= 365:
        errors.append("server.panel_session_days: must be in the range 1..365")
    if errors:
        raise ConfigError(errors)
    config = Config(
        pools=pools,
        routes=routes,
        callers=callers,
        tasks=tasks,
        backends=backends,
        statusline=statusline,
        classifier=classifier,
        storage=storage,
        sinks=sinks,
        port=port,
        counter_interval_s=counter_interval_s,
        snapshot_ttl_s=snapshot_ttl_s,
        models=models,
        upstreams=upstreams,
        panel_session_days=panel_session_days,
        usage=usage,
    )
    from agent_router import registry

    errors = registry.validate(config)
    if errors:
        raise ConfigError(errors)
    return config


def _mask_routes(text: str, found: Collection[str] | None) -> str:
    """Comment out every [[routes]] block whose backend harness is not in found."""
    keep = frozenset(found) if found is not None else frozenset()
    out: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line != "[[routes]]":
            out.append(line)
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and lines[index].strip():
            block.append(lines[index])
            index += 1
        backend = next(
            (
                item.split("=", 1)[1].strip().strip('"')
                for item in block
                if item.startswith("backend")
            ),
            None,
        )
        if backend in keep:
            out.extend(block)
        else:
            out.extend(f"# {item}" for item in block)
    return "\n".join(out) + "\n"


def default_config_text(found: Collection[str] | None = None) -> str:
    """Init template from the packaged example; routes of harnesses not in found are commented."""
    text = (
        resources.files("agent_router").joinpath("config.example.toml").read_text(encoding="utf-8")
    )
    return _mask_routes(text, found)


def home_dir() -> Path:
    override = os.environ.get("AGENT_ROUTER_HOME")
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir("agent-router"))
