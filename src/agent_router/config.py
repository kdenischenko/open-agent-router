"""TOML config: immutable model, template, and validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlparse

import platformdirs

from agent_router.clock import to_utc
from agent_router.periods import Duration, parse_duration
from agent_router.prices import PriceTable, load_prices

Tier = Literal["cheap", "standard", "strong"]
TIERS: tuple[Tier, ...] = ("cheap", "standard", "strong")
Mode = Literal["read", "write"]
SpendSource = Literal["counter", "journal", "proxy"]
RouteKind = Literal["harness", "api"]


def model_key(route_model: str) -> str:
    return route_model.split("#", 1)[0].split("/", 1)[-1]


def effort_of(route_model: str) -> str | None:
    _, separator, effort = route_model.partition("#")
    return effort if separator and effort else None


@dataclass(frozen=True)
class BackendSection:
    name: str
    harness: str
    executable: str
    priority: int | None = None
    weight: float | None = None


_STORAGE_BACKEND = re.compile(r"[a-z0-9_-]+")
_CMD_UNSAFE = frozenset('&|^%"\r\n')
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


@dataclass(frozen=True)
class Window:
    id: str
    duration: Duration
    kind: Literal["fixed", "rolling"]
    role: Literal["pace", "guard"]
    anchor: datetime | None
    limit_usd: float | None
    block_at: float


@dataclass(frozen=True)
class Pool:
    id: str
    title: str
    unit: Literal["percent", "usd"]
    windows: tuple[Window, ...]
    reserve: float
    reserve_tier: Tier | None
    counter: str | None
    counter_provider: str | None
    counter_model: str | None
    priority: int | None = None
    weight: float | None = None
    price_file: Path | None = None
    prices: PriceTable | None = None
    limit_base_usd: float | None = None
    model_limits: Mapping[str, float] = field(default_factory=dict)
    model_limits_date: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "windows", tuple(self.windows))
        object.__setattr__(
            self,
            "model_limits",
            MappingProxyType({key: float(value) for key, value in self.model_limits.items()}),
        )

    def scale(self, model: str) -> float:
        """Model weight in the Go counter: pool base / monthly model limit."""
        if self.limit_base_usd is None:
            return 1.0
        limit = self.model_limits.get(model_key(model))
        if limit is None:
            return 1.0
        return self.limit_base_usd / limit

    @property
    def scale_hash(self) -> str | None:
        """Fingerprint of the weight table: changes if limit_base_usd or model_limits change."""
        if self.limit_base_usd is None:
            return None
        payload = json.dumps([self.limit_base_usd, sorted(self.model_limits.items())])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Route:
    id: str
    pool: str
    tiers: frozenset[Tier]
    quality: int
    backend: str | None
    model: str | None
    modes: frozenset[Mode]
    confirm: bool
    enabled: bool
    timeout_s: int | None
    kind: RouteKind = "harness"
    upstream: str | None = None
    model_key: str | None = None
    priority: int | None = None
    weight: float | None = None
    spend: SpendSource | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tiers", frozenset(self.tiers))
        object.__setattr__(self, "modes", frozenset(self.modes))

    @property
    def model_group(self) -> str:
        return self.model_key or self.id


@dataclass(frozen=True)
class TaskSettings:
    allowed_roots: tuple[Path, ...]
    denied_roots: tuple[Path, ...]
    max_parallel: int = 3
    max_queued: int = 200
    max_attempts: int = 3
    default_timeout_s: int = 14400
    idle_timeout_s: int = 600
    approval_timeout_s: int = 600
    limit_backoff_s: int = 1800
    limit_backoff_max_s: int = 18000
    max_prompt_chars: int = 200_000
    exit_grace_s: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_roots", tuple(self.allowed_roots))
        object.__setattr__(self, "denied_roots", tuple(self.denied_roots))


@dataclass(frozen=True)
class StatuslineSettings:
    pool: str
    five_hour: str = "5h"
    seven_day: str = "week"


@dataclass(frozen=True)
class ClassifierModel:
    """One classifier endpoint: systemone, Ollama, or OpenAI-compatible chat."""

    id: str
    kind: Literal["systemone", "ollama", "openai"]
    url: str
    model: str
    timeout_s: float
    live: bool = True
    min_confidence: float | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class ClassifierSettings:
    kind: Literal["none", "systemone", "ollama", "openai"] = "none"
    url: str = "http://localhost:11434"
    model: str = "tev1:4b-q4_K_M"
    timeout_s: float = 3
    api_key_env: str | None = None
    mode: Literal["shadow", "active"] = "shadow"
    primary: str | None = None
    min_confidence: float = 0.6
    write_floor: Tier = "standard"
    wait_s: float | None = None
    vram_budget_mb: int = 5300
    models: tuple[ClassifierModel, ...] = ()

    @property
    def effective_models(self) -> tuple[ClassifierModel, ...]:
        """Models in the new form; the legacy form is read as one classifier with id = kind."""
        if self.models:
            return self.models
        if self.kind == "none":
            return ()
        return (
            ClassifierModel(
                id=self.kind,
                kind=self.kind,  # type: ignore[arg-type]
                url=self.url,
                model=self.model,
                timeout_s=self.timeout_s,
                api_key_env=self.api_key_env,
            ),
        )

    @property
    def primary_model(self) -> ClassifierModel | None:
        """The model whose vote decides in active mode and is written to classifier_tier."""
        models = self.effective_models
        if not models:
            return None
        return next((item for item in models if item.id == self.primary), models[0])


@dataclass(frozen=True)
class StorageSettings:
    backend: str = "sqlite"
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class SinkSettings:
    type: str
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class ModelSettings:
    prefer: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefer", tuple(self.prefer))


@dataclass(frozen=True)
class UpstreamSettings:
    name: str
    kind: str
    pool: str
    base_url: str
    key_file: str | None
    key_env: str | None
    pass_headers: tuple[str, ...]
    stream_usage: bool = True
    enabled: bool = True
    priority: int | None = None
    weight: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pass_headers", tuple(self.pass_headers))


@dataclass(frozen=True)
class Config:
    pools: tuple[Pool, ...]
    routes: tuple[Route, ...]
    callers: Mapping[str, str | None]
    tasks: TaskSettings
    backends: Mapping[str, BackendSection]
    statusline: StatuslineSettings | None
    classifier: ClassifierSettings
    storage: StorageSettings
    sinks: tuple[SinkSettings, ...]
    port: int = 8787
    counter_interval_s: int = 300
    snapshot_ttl_s: float = 1.0
    models: Mapping[str, ModelSettings] = field(default_factory=dict)
    upstreams: Mapping[str, UpstreamSettings] = field(default_factory=dict)
    panel_session_days: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "sinks", tuple(self.sinks))
        object.__setattr__(self, "callers", MappingProxyType(dict(self.callers)))
        object.__setattr__(self, "backends", MappingProxyType(dict(self.backends)))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        object.__setattr__(self, "upstreams", MappingProxyType(dict(self.upstreams)))

    def backend_section(self, name: str) -> BackendSection:
        return self.backends.get(name) or BackendSection(name, name, "")

    def levers(self, route: Route) -> tuple[int, float]:
        if route.kind == "api":
            section = self.upstreams.get(route.upstream) if route.upstream is not None else None
        else:
            section = self.backend_section(route.backend) if route.backend is not None else None
        pool = next((item for item in self.pools if item.id == route.pool), None)
        priority = next(
            (
                value
                for value in (
                    route.priority,
                    section.priority if section is not None else None,
                    pool.priority if pool is not None else None,
                )
                if value is not None
            ),
            0,
        )
        weight = next(
            (
                value
                for value in (
                    route.weight,
                    section.weight if section is not None else None,
                    pool.weight if pool is not None else None,
                )
                if value is not None
            ),
            0.0,
        )
        return priority, float(weight)

    def route_spend(self, route: Route) -> SpendSource:
        if route.kind == "api":
            return "proxy"
        if route.spend is not None:
            return route.spend
        if route.backend is None:
            return "journal"
        from agent_router import registry

        return registry.load_harness(self.backend_section(route.backend).harness).spend


class ConfigError(Exception):
    messages: list[str]

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def _check_keys(
    raw: dict[str, Any], allowed: frozenset[str], label: str, errors: list[str]
) -> None:
    for key in raw:
        if key not in allowed:
            errors.append(f"{label}.{key}: unknown key")


def _table(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}: expected a table")
        return {}
    return value


def _tables(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        errors.append(f"{label}: expected a list of tables")
        return []
    return value


def _text(value: Any, label: str, errors: list[str], default: str = "") -> str:
    if isinstance(value, str) and value:
        return value
    errors.append(f"{label}: a non-empty string is required")
    return default


def _optional_text(value: Any, label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    errors.append(f"{label}: expected a string")
    return None


def _choice(
    value: Any, choices: tuple[str, ...], label: str, errors: list[str], default: str
) -> str:
    if value in choices:
        return value
    errors.append(f"{label}: invalid value: {value!r}; allowed: {', '.join(choices)}")
    return default


def _number(value: Any, label: str, errors: list[str], default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        if math.isfinite(value):
            return float(value)
        errors.append(f"{label}: expected a finite number")  # TOML allows nan and inf
        return default
    errors.append(f"{label}: expected a number")
    return default


def _integer(value: Any, label: str, errors: list[str], default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    errors.append(f"{label}: expected an integer")
    return default


def _boolean(value: Any, label: str, errors: list[str], default: bool) -> bool:
    if isinstance(value, bool):
        return value
    errors.append(f"{label}: expected true or false")
    return default


def _strings(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{label}: expected a list of strings")
        return []
    return value


def _paths(value: Any, label: str, errors: list[str]) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser() for item in _strings(value, label, errors))


def _check_cmd_text(value: str | None, label: str, errors: list[str]) -> None:
    if value is not None and any(character in value for character in _CMD_UNSAFE):
        errors.append(f"{label}: characters unsafe for .cmd are not allowed")


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
    }
)


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


def _required_text(value: Any, label: str, errors: list[str]) -> str:
    if value is None:
        errors.append(f"{label}: is required")
        return ""
    return _text(value, label, errors)


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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_api_key_env(value: Any, label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _ENV_NAME.fullmatch(value) is not None:
        return value
    errors.append(f"{label}: not a valid environment variable name")
    return None


def _check_classifier_address(
    url: str, api_key_env: str | None, label: str, errors: list[str]
) -> None:
    try:
        parsed = urlparse(url)
        _ = parsed.port  # raises on a port that is not a number in 0..65535
    except ValueError:
        parsed = None
    host = parsed.hostname if parsed is not None else None
    if host in _LOOPBACK_HOSTS:
        return
    if parsed is not None and parsed.scheme == "https" and api_key_env:
        return
    errors.append(f"{label}: address must be loopback, or https with api_key_env")


_CLASSIFIER_MODEL_KEYS = frozenset(
    {"id", "kind", "url", "model", "timeout_s", "live", "min_confidence", "api_key_env"}
)

_CLASSIFIER_KINDS = ("systemone", "ollama", "openai")


def _parse_classifier_common(raw: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """Settings shared by the single-model form and [[classifier.models]]."""
    mode = _choice(
        raw.get("mode", "shadow"), ("shadow", "active"), "classifier.mode", errors, "shadow"
    )
    min_confidence = _number(
        raw.get("min_confidence", 0.6), "classifier.min_confidence", errors, 0.6
    )
    if not 0 <= min_confidence <= 1:
        errors.append("classifier.min_confidence: must be in the range 0..1")
    write_floor = _choice(
        raw.get("write_floor", "standard"), TIERS, "classifier.write_floor", errors, "standard"
    )
    wait_s = None
    if raw.get("wait_s") is not None:
        wait_s = _number(raw.get("wait_s"), "classifier.wait_s", errors, 0.0)
        if wait_s < 0:
            errors.append("classifier.wait_s: cannot be negative")
    vram_budget_mb = _integer(
        raw.get("vram_budget_mb", 5300), "classifier.vram_budget_mb", errors, 5300
    )
    if vram_budget_mb <= 0:
        errors.append("classifier.vram_budget_mb: must be positive")
    return {
        "mode": mode,
        "min_confidence": min_confidence,
        "write_floor": write_floor,
        "wait_s": wait_s,
        "vram_budget_mb": vram_budget_mb,
    }


def _parse_classifier_models(raw: dict[str, Any], errors: list[str]) -> ClassifierSettings:
    for key in ("kind", "url", "model", "timeout_s", "api_key_env"):
        if key in raw:
            errors.append("classifier: old form and [[classifier.models]] used together")
            break
    common = _parse_classifier_common(raw, errors)
    raw_models = raw.get("models") or []
    if not isinstance(raw_models, list) or any(not isinstance(item, dict) for item in raw_models):
        errors.append("classifier.models: expected a list of tables")
        raw_models = []
    models: list[ClassifierModel] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_models):
        label = f"classifier.models[{index}]"
        _check_keys(item, _CLASSIFIER_MODEL_KEYS, label, errors)
        model_id = _text(item.get("id"), f"{label}.id", errors)
        if model_id in seen:
            errors.append(f"{label}.id: duplicate id: {model_id!r}")
        seen.add(model_id)
        kind = _choice(item.get("kind"), _CLASSIFIER_KINDS, f"{label}.kind", errors, "systemone")
        url = _text(item.get("url", "http://localhost:11434"), f"{label}.url", errors)
        raw_key_env = item.get("api_key_env")
        address_key_env = raw_key_env if isinstance(raw_key_env, str) and raw_key_env else None
        _check_classifier_address(url, address_key_env, f"{label}.url", errors)
        api_key_env = _check_api_key_env(raw_key_env, f"{label}.api_key_env", errors)
        model = _text(item.get("model"), f"{label}.model", errors)
        timeout_s = _number(item.get("timeout_s", 3), f"{label}.timeout_s", errors, 3)
        live = _boolean(item.get("live", True), f"{label}.live", errors, True)
        model_min: float | None = None
        if item.get("min_confidence") is not None:
            model_min = _number(item.get("min_confidence"), f"{label}.min_confidence", errors, 0.0)
            if not 0 <= model_min <= 1:
                errors.append(f"{label}.min_confidence: must be in the range 0..1")
                model_min = None
        models.append(
            ClassifierModel(
                id=model_id,
                kind=kind,
                url=url,
                model=model,
                timeout_s=timeout_s,
                live=live,
                min_confidence=model_min,
                api_key_env=api_key_env,
            )
        )
    primary = raw.get("primary")
    if primary is not None and not isinstance(primary, str):
        errors.append("classifier.primary: a non-empty string is required")
        primary = None
    if primary is None and models:
        primary = models[0].id
    if primary is not None and primary not in seen:
        errors.append(f"classifier.primary: unknown classifier: {primary!r}")
    # legacy fields come from the deciding model; without an explicit primary it is also the first
    chosen = next((item for item in models if item.id == primary), None)
    if chosen is not None and not chosen.live:
        errors.append(
            f"classifier.primary: deciding model {chosen.id!r} is disabled (live = false)"
        )
    return ClassifierSettings(
        kind=chosen.kind if chosen is not None else "none",
        url=chosen.url if chosen is not None else "http://localhost:11434",
        model=chosen.model if chosen is not None else "tev1:4b-q4_K_M",
        timeout_s=chosen.timeout_s if chosen is not None else 3,
        api_key_env=chosen.api_key_env if chosen is not None else None,
        primary=primary,
        models=tuple(models),
        **common,
    )


_CLASSIFIER_KEYS = frozenset(
    {
        "kind",
        "url",
        "model",
        "timeout_s",
        "mode",
        "min_confidence",
        "write_floor",
        "wait_s",
        "vram_budget_mb",
        "models",
        "primary",
        "api_key_env",
    }
)


def _parse_classifier(value: Any, errors: list[str]) -> ClassifierSettings:
    raw = _table(value, "classifier", errors)
    _check_keys(raw, _CLASSIFIER_KEYS, "classifier", errors)
    if "models" in raw:
        return _parse_classifier_models(raw, errors)
    url = _text(raw.get("url", "http://localhost:11434"), "classifier.url", errors)
    raw_key_env = raw.get("api_key_env")
    address_key_env = raw_key_env if isinstance(raw_key_env, str) and raw_key_env else None
    _check_classifier_address(url, address_key_env, "classifier.url", errors)
    api_key_env = _check_api_key_env(raw_key_env, "classifier.api_key_env", errors)
    common = _parse_classifier_common(raw, errors)
    return ClassifierSettings(
        kind=_choice(
            raw.get("kind", "none"),
            ("none",) + _CLASSIFIER_KINDS,
            "classifier.kind",
            errors,
            "none",
        ),
        url=url,
        model=_text(raw.get("model", "tev1:4b-q4_K_M"), "classifier.model", errors),
        timeout_s=_number(raw.get("timeout_s", 3), "classifier.timeout_s", errors, 3),
        api_key_env=api_key_env,
        **common,
    )


def _parse_storage(value: Any, errors: list[str]) -> StorageSettings:
    raw = _table(value, "storage", errors)
    backend = _text(raw.get("backend", "sqlite"), "storage.backend", errors, "sqlite")
    if _STORAGE_BACKEND.fullmatch(backend) is None:
        errors.append("storage.backend: only [a-z0-9_-]+ characters are allowed")
    return StorageSettings(
        backend=backend, options={key: item for key, item in raw.items() if key != "backend"}
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
    }
)
_SERVER_KEYS = frozenset({"port", "counter_interval_s", "snapshot_ttl_s", "panel_session_days"})


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
