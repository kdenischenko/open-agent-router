"""Reading usage sources on a schedule and on demand.

The reader turns each source read into observations through the same write path manual
calibration uses, publishes one event per pool on the bus, and keeps a small in-memory
state per pool for display. Events and results carry ids and reason codes only: never
keys or response bodies.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from agent_router import registry
from agent_router.clock import Clock
from agent_router.config import ConfigError
from agent_router.config_model import Config
from agent_router.events import EventBus
from agent_router.usage import UsageRead, UsageSource, UsageSourceSpec, observations

MIN_REFRESH_S = 60  # one read per pool per minute at most, scheduled or forced


class UnknownPoolError(Exception):
    """Refresh named a pool the config does not know; the API answers 422 unknown_pool."""


@dataclass(frozen=True)
class UsageReadState:
    """What the attempts of one pool's source left behind; moments are UTC."""

    pool: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_reason: str | None = None  # None after a success


@dataclass(frozen=True)
class PoolRead:
    """A pool whose source was read: the windows the observations were written for."""

    pool: str
    windows: tuple[str, ...]


@dataclass(frozen=True)
class PoolOutcome:
    """A pool that was not read: why it failed or why the refresh skipped it."""

    pool: str
    reason: str


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of one refresh pass; to_json is the shape of the API response."""

    read: tuple[PoolRead, ...] = ()
    failed: tuple[PoolOutcome, ...] = ()
    skipped: tuple[PoolOutcome, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "read": [{"pool": item.pool, "windows": list(item.windows)} for item in self.read],
            "failed": [{"pool": item.pool, "reason": item.reason} for item in self.failed],
            "skipped": [{"pool": item.pool, "reason": item.reason} for item in self.skipped],
        }


class UsageReader:
    """Builds one usage source per configured pool and reads them when asked.

    ``gateway`` provides the write path shared with manual calibration:
    ``record_observation`` under ``write_lock``. ``sources`` replaces the built
    sources per pool id and exists for tests.
    """

    def __init__(
        self,
        config: Config,
        gateway: Any,
        clock: Clock,
        bus: EventBus,
        environ: Mapping[str, str] | None = None,
        sources: Mapping[str, UsageSource] | None = None,
    ) -> None:
        if environ is None:
            environ = os.environ
        self._config = config
        self._gateway = gateway
        self._clock = clock
        self._bus = bus
        self._specs: dict[str, UsageSourceSpec] = {}
        self._sources: dict[str, UsageSource] = {}
        for pool in config.pools:
            name = pool.usage.source
            if name is None:
                continue
            try:
                spec = registry.load_usage_source(name)
            except ConfigError:
                continue  # config validation already reports this; the reader stays usable
            source = None if sources is None else sources.get(pool.id)
            if source is None:
                try:
                    source = spec.factory(pool.usage, environ)
                except Exception:
                    continue  # a source that cannot be built must not stop the gateway
            self._specs[pool.id] = spec
            self._sources[pool.id] = source
        self._lock = threading.Lock()
        self._state: dict[str, UsageReadState] = {
            pool_id: UsageReadState(pool_id) for pool_id in self._sources
        }

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    def state(self, pool_id: str) -> UsageReadState | None:
        """Per-pool read state, or None for a pool without a usable source."""
        with self._lock:
            return self._state.get(pool_id)

    def refresh(self, pool_id: str | None = None, *, scheduled: bool = False) -> RefreshResult:
        """Read one pool or every pool with a source; rate-limited per pool.

        A scheduled pass additionally waits out ``[usage].interval_s`` since the last
        attempt, so the background thread can tick faster than the interval.
        """
        now = self._clock.now()
        if pool_id is not None:
            if not any(pool.id == pool_id for pool in self._config.pools):
                raise UnknownPoolError(f"unknown pool: {pool_id}")
            candidates = (pool_id,)
        else:
            candidates = tuple(pool.id for pool in self._config.pools if pool.id in self._sources)
        read: list[PoolRead] = []
        failed: list[PoolOutcome] = []
        skipped: list[PoolOutcome] = []
        for candidate in candidates:
            source = self._sources.get(candidate)
            if source is None:
                skipped.append(PoolOutcome(candidate, "no_source"))
                continue
            if self._config.usage_mode(candidate) == "manual":
                skipped.append(PoolOutcome(candidate, "manual"))
                continue
            if not self._reserve(candidate, now, scheduled):
                skipped.append(PoolOutcome(candidate, "too_soon"))
                continue
            self._read(candidate, source, now, read, failed)
        return RefreshResult(tuple(read), tuple(failed), tuple(skipped))

    def _reserve(self, pool_id: str, now: datetime, scheduled: bool) -> bool:
        """Claim the pool's read slot; False when it was read too recently.

        The check and the new attempt time are one step under the lock, so a refresh that
        arrives while another one is reading the same pool is skipped instead of reading twice.
        """
        limit = max(MIN_REFRESH_S, self._config.usage.interval_s) if scheduled else MIN_REFRESH_S
        with self._lock:
            previous = self._state[pool_id]
            last = previous.last_attempt_at
            if last is not None and (now - last).total_seconds() < limit:
                return False
            self._state[pool_id] = replace(previous, last_attempt_at=now)
            return True

    def _read(
        self,
        pool_id: str,
        source: UsageSource,
        now: datetime,
        read: list[PoolRead],
        failed: list[PoolOutcome],
    ) -> None:
        pool = next(item for item in self._config.pools if item.id == pool_id)
        spec = self._specs[pool_id]
        try:
            result = source.read()
        except Exception:
            result = UsageRead(reason="error")  # a crashing source is a failed read
        found = observations(pool, spec, result, now)
        if result.ok and not found:
            # nothing the pool maps was usable: report it rather than a success that wrote nothing
            result = UsageRead(reason="no_data")
        if not result.ok:
            reason = result.reason or "error"
            self._mark(pool_id, now, reason)
            failed.append(PoolOutcome(pool_id, reason))
            self._bus.publish(
                "usage.read_failed",
                {"pool": pool_id, "source": spec.name, "reason": reason},
            )
            return
        for obs in found:
            with self._gateway.write_lock:
                self._gateway.record_observation(obs)
        self._mark(pool_id, now, None)
        for hook in list(self._gateway.after_calibration):
            # the same out-of-schedule counter check a manual calibration triggers
            with contextlib.suppress(Exception):
                hook(pool_id)
        windows = [obs.window for obs in found]
        read.append(PoolRead(pool_id, tuple(windows)))
        self._bus.publish("usage.read", {"pool": pool_id, "source": spec.name, "windows": windows})

    def _mark(self, pool_id: str, now: datetime, reason: str | None) -> None:
        with self._lock:
            previous = self._state[pool_id]
            self._state[pool_id] = UsageReadState(
                pool=pool_id,
                last_attempt_at=now,
                last_success_at=now if reason is None else previous.last_success_at,
                last_reason=reason,
            )
