"""Registry of harnesses and counters discovered via Python entry points."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agent_router import plugins
from agent_router.config import BackendSection, Config, ConfigError, SpendSource
from agent_router.runner import ResolvedExecutable, resolve_executable

if TYPE_CHECKING:
    from agent_router.adapters.base import Adapter
    from agent_router.clock import Clock
    from agent_router.events import EventBus
    from agent_router.store import Store

HARNESS_GROUP = "agent_router.harnesses"
COUNTER_GROUP = "agent_router.counters"


@dataclass(frozen=True)
class HarnessSpec:
    name: str
    adapter: Adapter
    executables: tuple[str, ...]
    spend: SpendSource
    proxy_capable: bool = False


class Counter(Protocol):
    def __init__(self, config: Config, store: Store, clock: Clock, bus: EventBus) -> None: ...

    def sample_once(self) -> None: ...


@dataclass(frozen=True)
class CounterSpec:
    name: str
    factory: Callable[[Config, Store, Clock, EventBus], Counter]
    sees: frozenset[str]


_OVERRIDES: dict[str, HarnessSpec] = {}
_HARNESSES: dict[str, HarnessSpec] = {}
_COUNTERS: dict[str, CounterSpec] = {}


def _entry_names(group: str) -> set[str]:
    return {entry.name for entry in plugins.metadata.entry_points(group=group)}


def _available(group: str, overrides: dict[str, object] | None = None) -> str:
    names = _entry_names(group) | set(overrides or {})
    return ", ".join(sorted(names)) or "none"


def load_harness(name: str) -> HarnessSpec:
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    if name in _HARNESSES:
        return _HARNESSES[name]
    try:
        loaded = plugins.load_plugin(HARNESS_GROUP, name)
    except ConfigError as exc:
        if name not in _entry_names(HARNESS_GROUP):
            available = _available(HARNESS_GROUP, _OVERRIDES)
            raise ConfigError([f'unknown harness "{name}"; available: {available}']) from exc
        raise
    if not isinstance(loaded, HarnessSpec):
        raise ConfigError([f'plugin "{name}" in group {HARNESS_GROUP} has the wrong type'])
    _HARNESSES[name] = loaded
    return loaded


def load_counter(name: str) -> CounterSpec:
    if name in _COUNTERS:
        return _COUNTERS[name]
    try:
        loaded = plugins.load_plugin(COUNTER_GROUP, name)
    except ConfigError as exc:
        if name not in _entry_names(COUNTER_GROUP):
            available = _available(COUNTER_GROUP)
            raise ConfigError([f'unknown counter "{name}"; available: {available}']) from exc
        raise
    if not isinstance(loaded, CounterSpec):
        raise ConfigError([f'plugin "{name}" in group {COUNTER_GROUP} has the wrong type'])
    _COUNTERS[name] = loaded
    return loaded


def _sections(config: Config) -> dict[str, BackendSection]:
    sections = {
        route.backend: config.backend_section(route.backend)
        for route in config.routes
        if route.backend is not None
    }
    for name in config.backends:
        sections[name] = config.backend_section(name)
    return sections


def harnesses(config: Config) -> dict[str, HarnessSpec]:
    return {name: load_harness(section.harness) for name, section in _sections(config).items()}


def validate(config: Config) -> list[str]:
    errors: list[str] = []
    for name, section in _sections(config).items():
        try:
            load_harness(section.harness)
        except ConfigError as exc:
            errors.extend(f"backends.{name}.harness: {message}" for message in exc.messages)
    for index, pool in enumerate(config.pools):
        if pool.counter is None:
            continue
        try:
            load_counter(pool.counter)
        except ConfigError as exc:
            errors.extend(f"pools[{index}].counter: {message}" for message in exc.messages)
    for index, route in enumerate(config.routes):
        if route.kind != "harness" or route.spend not in ("counter", "proxy"):
            continue
        pool = next((item for item in config.pools if item.id == route.pool), None)
        if pool is None:
            continue
        section = config.backend_section(route.backend) if route.backend is not None else None
        try:
            harness = load_harness(section.harness) if section is not None else None
        except ConfigError:
            continue
        if route.spend == "proxy":
            if harness is not None and not harness.proxy_capable:
                reason = f"harness {section.harness} does not work through the proxy"
                errors.append(f"routes[{index}].spend: {reason}")
            continue
        if pool.counter is None:
            errors.append(f"routes[{index}].spend: pool {pool.id} has no counter")
            continue
        try:
            counter = load_counter(pool.counter)
        except ConfigError:
            continue
        if section is not None and section.harness not in counter.sees:
            reason = f"counter {pool.counter} does not see harness {section.harness}"
            errors.append(f"routes[{index}].spend: {reason}")
    return errors


def resolve_backend(
    section: BackendSection,
    spec: HarnessSpec,
    resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
) -> ResolvedExecutable:
    if section.executable:
        return resolve(section.executable)
    result = ResolvedExecutable(None, False)
    for name in spec.executables:
        result = resolve(name)
        if result.path is not None:
            return result
    return result
