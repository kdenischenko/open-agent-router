"""Immutable configuration data model: type aliases, settings dataclasses, and Config."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from agent_router.periods import Duration, approx_length
from agent_router.prices import PriceTable

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


@dataclass(frozen=True)
class UsageSettings:
    """Root [usage] table: how the gateway reads usage sources."""

    mode: str = "auto"  # "auto" reads sources on the interval; "manual" is calibration only
    interval_s: int = 3600
    # whether the config had a [usage] table at all: doctor stays quiet about pools
    # without a source unless the user opted into usage tracking here
    present: bool = False


@dataclass(frozen=True)
class PoolUsage:
    """A pool's [pools.usage] sub-table: which source feeds its windows."""

    source: str | None = None
    mode: str | None = None  # None inherits [usage].mode
    api_key_env: str | None = None
    executable: str | None = None  # CLI a source runs; None uses the source's default
    windows: Mapping[str, str] = field(default_factory=dict)  # source field -> window id

    def __post_init__(self) -> None:
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))


@dataclass(frozen=True)
class Window:
    id: str
    duration: Duration
    kind: Literal["fixed", "rolling"]
    role: Literal["pace", "guard"]
    anchor: datetime | None
    limit_usd: float | None
    block_at: float
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        if self.max_age is None:
            # Half a window tolerates collection gaps; the 72h cap bounds staleness on long windows.
            default = min(approx_length(self.duration) / 2, timedelta(hours=72))
            object.__setattr__(self, "max_age", default)


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
    usage: PoolUsage = field(default_factory=PoolUsage)

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
    retention_days: int | None = None
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
    usage: UsageSettings = field(default_factory=UsageSettings)

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

    def usage_mode(self, pool_id: str) -> str:
        """Effective usage mode of a pool: its own override, else the root [usage].mode."""
        pool = next((item for item in self.pools if item.id == pool_id), None)
        if pool is not None and pool.usage.mode is not None:
            return pool.usage.mode
        return self.usage.mode

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
