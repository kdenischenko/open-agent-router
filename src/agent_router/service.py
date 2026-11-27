"""Snapshot assembly, snapshot cache, and advice: wires the store to the core's pure functions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_router import registry
from agent_router.auth import PanelAuth, Tokens
from agent_router.classifier import snapshot_classifier
from agent_router.clock import Clock, to_utc
from agent_router.config import Config, ConfigError, Mode, Pool, Tier, UsageSettings
from agent_router.events import Event, EventBus
from agent_router.jobstore import JobStore, migrate_jobs_schema
from agent_router.ledger import window_state
from agent_router.pacing import PoolStatus, WindowPace, pool_status
from agent_router.policy import (
    Decision,
    PoolBlock,
    RouteRequest,
    RouteView,
    Snapshot,
    decide,
)
from agent_router.runner import LocalRunner
from agent_router.settings_store import SqliteSettings
from agent_router.spend import SpendReader
from agent_router.store import Observation, SqliteStore, Store
from agent_router.usage import is_stale

if TYPE_CHECKING:
    from agent_router.dispatcher import Dispatcher
    from agent_router.usage_reader import UsageReadState

log = logging.getLogger(__name__)


def build_snapshot(
    config: Config,
    store: Store,
    now: datetime,
    tz: tzinfo | None,
    blocks: Mapping[str, PoolBlock] | None = None,
    spend: SpendReader | None = None,
) -> Snapshot:
    """State of all pools at now; pool closures (blocks) include only those still in effect."""
    now = to_utc(now)
    tolerance = timedelta(seconds=3 * config.counter_interval_s)
    pools: dict[str, PoolStatus] = {}
    for pool in config.pools:
        states = [
            window_state(pool, window, store, now, tolerance, spend=spend)
            for window in pool.windows
        ]
        pools[pool.id] = pool_status(pool, states, now)
    active = {key: block for key, block in (blocks or {}).items() if block.until > now}
    return Snapshot(now=now, tz=tz, pools=pools, blocks=active)


class SnapshotCache:
    """The snapshot is recomputed when an event appears on the bus or the TTL expires."""

    def __init__(
        self, bus: EventBus, clock: Clock, ttl_s: float, build: Callable[[datetime], Snapshot]
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._ttl_s = ttl_s
        self._build = build
        self._lock = threading.Lock()  # called by request threads and the dispatcher
        self._snapshot: Snapshot | None = None
        self._seq = -1
        self._built_at: datetime | None = None

    def get(self) -> Snapshot:
        with self._lock:
            now = self._clock.now()
            seq = self._bus.seq  # seq BEFORE the build: a write during the build forces a recompute
            fresh = (
                self._snapshot is not None
                and self._built_at is not None
                and seq == self._seq
                and 0 <= (now - self._built_at).total_seconds() < self._ttl_s
            )
            if not fresh:
                self._snapshot = self._build(now)
                self._seq = seq
                self._built_at = now
            assert self._snapshot is not None
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None


def route_views(
    config: Config, availability: Mapping[str, str | None] | None = None
) -> list[RouteView]:
    """Config routes with harness availability: route name -> unavailability reason or None."""
    views = []
    for route in config.routes:
        reason = (availability or {}).get(route.id)
        priority, weight = config.levers(route)
        model = config.models.get(route.model_group)
        prefer_rank = (
            model.prefer.index(route.pool)
            if model is not None and route.pool in model.prefer
            else None
        )
        views.append(
            RouteView(
                route=route,
                available=reason is None,
                unavailable_reason=reason,
                priority=priority,
                weight=weight,
                prefer_rank=prefer_rank,
            )
        )
    return views


def review_targets(config: Config, route_id: str | None) -> tuple[str | None, str | None]:
    """Model group and pool that a review of a job on this route avoids; (None, None) when the
    reviewed job has no route yet or the route is no longer configured."""
    route = next((route for route in config.routes if route.id == route_id), None)
    if route is None:
        return None, None
    return route.model_group, route.pool


def advise(
    config: Config,
    snapshot: Snapshot,
    tier: Tier,
    caller: str | None,
    mode: Mode | None = None,
    views: Sequence[RouteView] | None = None,
    exclude_pools: frozenset[str] = frozenset(),
    reviewed_route: str | None = None,
) -> Decision:
    """Advice on where to send; without a mode, routes of either mode match.

    The caller's pool comes from [callers]. `reviewed_route` is the route of the job being
    reviewed: its model group and pool are excluded.
    """
    caller_pool = config.callers.get(caller) if caller else None
    model_group, pool = review_targets(config, reviewed_route)
    request = RouteRequest(
        tier=tier,
        mode=mode,
        caller_pool=caller_pool,
        request_excluded_pools=exclude_pools,
        kind="harness",
        reviewed_model_group=model_group,
        reviewed_pool=pool,
    )
    return decide(request, snapshot, route_views(config) if views is None else views)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else to_utc(moment).isoformat()


def _window_json(pace: WindowPace, now: datetime, observation_source: str | None) -> dict[str, Any]:
    state = pace.state
    age = None if state.observed_at is None else max(0.0, (now - state.observed_at).total_seconds())
    return {
        "id": pace.window.id,
        "kind": pace.window.kind,
        "role": pace.window.role,
        "known": state.known,
        "used": state.used,
        "expected": pace.expected,
        "lag": pace.lag,
        "urgency": pace.urgency,
        "resets_at": _iso(state.resets_at),
        "source": state.source,
        "figure_source": _figure_source(state.source, observation_source),
        "observed_at": _iso(state.observed_at),
        "age_s": age,
        "estimated": state.estimated,
        "estimated_prices": state.estimated_prices,
        "blocked": pace.blocked,
        "note": state.note,
        "anchored": pace.window.anchor is not None,
    }


def _figure_source(state_source: str | None, observation_source: str | None) -> str:
    """Where a window's figure comes from: the pool's usage source, manual entry, or neither.

    "journal" covers every other case — no observation in the current window, a
    scheduled reset, or a collector that is neither the pool's usage source nor a
    human calibration.
    """
    if observation_source is not None and state_source == observation_source:
        return "auto"
    if state_source == "manual":
        return "manual"
    return "journal"


def _pool_observation_source(pool: Pool) -> str | None:
    """Observation source name of the pool's usage source; None when the pool has none."""
    if pool.usage.source is None:
        return None
    try:
        return registry.load_usage_source(pool.usage.source).observation_source
    except ConfigError:
        return None  # config validation rejects unknown sources; the snapshot stays buildable


def _usage_json(
    pool: Pool,
    config: Config | None,
    states: Mapping[str, UsageReadState] | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Read health of the pool's usage source; None when the pool has no source."""
    if pool.usage.source is None:
        return None
    settings = config.usage if config is not None else UsageSettings()
    mode = config.usage_mode(pool.id) if config is not None else settings.mode
    state = None if states is None else states.get(pool.id)
    last_success = None if state is None else state.last_success_at
    age = None if last_success is None else max(0.0, (now - last_success).total_seconds())
    return {
        "source": pool.usage.source,
        "mode": mode,
        "last_attempt_at": _iso(None if state is None else state.last_attempt_at),
        "last_success_at": _iso(last_success),
        "last_error": None if state is None else state.last_reason,
        "warning": None if state is None else state.last_warning,
        "age_s": age,
        # auto mode promises reads on the interval; no success within two intervals is stale
        "stale": mode == "auto" and is_stale(state, now, settings.interval_s),
        "missing_fields": list(() if state is None else state.missing_fields),
        "format_changed": False if state is None else state.format_changed,
    }


def _via_gateway(jobs: JobStore, status: PoolStatus) -> dict[str, Any]:
    """Pool spend via the gateway since the latest observation of any of its windows."""
    seen = [pace.state.observed_at for pace in status.windows if pace.state.observed_at]
    since = max(seen) if seen else None
    usage = jobs.usage_since(status.pool.id, since)
    spend = jobs.spend_since(status.pool.id, since)
    return {
        "since": _iso(since),
        "jobs": usage.jobs,
        "minutes": round(usage.seconds / 60, 1),
        "requests": spend.requests,
        "tokens": usage.tokens + usage.proxy_tokens,
        "cost_usd": spend.cost_usd,
    }


def snapshot_json(
    snapshot: Snapshot,
    store: Store,
    jobs: JobStore | None = None,
    config: Config | None = None,
    usage_states: Mapping[str, UsageReadState] | None = None,
) -> dict[str, Any]:
    pools = []
    for status in snapshot.pools.values():
        pool = status.pool
        block = snapshot.blocks.get(pool.id)
        sampled = store.last_counter(pool.id) if pool.counter else None
        observation_source = _pool_observation_source(pool)
        pools.append(
            {
                "id": pool.id,
                "title": pool.title,
                "unit": pool.unit,
                "known": status.known,
                "lag": status.lag,
                "urgency": status.urgency,
                "blocked_by": status.blocked_by,
                "blocked_until": _iso(status.blocked_until),
                "reserve_hit": status.reserve_hit,
                "block": None
                if block is None
                else {"until": _iso(block.until), "reason": block.reason},
                "via_gateway": None if jobs is None else _via_gateway(jobs, status),
                "counter_sampled_at": None if sampled is None else _iso(sampled.sampled_at),
                "windows": [
                    _window_json(pace, snapshot.now, observation_source) for pace in status.windows
                ],
                "usage": _usage_json(pool, config, usage_states, snapshot.now),
            }
        )
    result = {"now": _iso(snapshot.now), "pools": pools}
    if config is not None and config.classifier.kind != "none":
        matched, compared = (0, 0) if jobs is None else jobs.classifier_agreement()
        result["classifier"] = snapshot_classifier(config.classifier, (matched, compared))
    return result


def decision_json(tier: Tier, decision: Decision, mode: Mode | None = None) -> dict[str, Any]:
    def item(entry: Any) -> dict[str, Any]:
        return {
            "route": entry.route.id,
            "pool": entry.route.pool,
            "reason": entry.reason,
            "note": getattr(entry, "note", None),
        }

    return {
        "tier": tier,
        "mode": mode,
        "chosen": [item(entry) for entry in decision.chosen],
        "excluded": [item(entry) for entry in decision.excluded],
        "note": decision.note,
    }


def record_classifier_mode(settings: SqliteSettings, mode: str, now: datetime) -> None:
    """Moment of a classifier mode change, stored as `"<mode>|<ISO-moment>"`.

    The same mode leaves the value untouched - the string stays from the first change.
    """
    raw = settings.get("classifier_mode_since")
    current = raw.partition("|")[0] if raw else None
    if current == mode:
        return
    settings.set("classifier_mode_since", f"{mode}|{to_utc(now).isoformat()}")


class Gateway:
    """Shared gateway state for the API and dashboard: config, store, bus, snapshot cache."""

    def __init__(
        self,
        config: Config,
        store: Store,
        clock: Clock,
        tokens: Tokens,
        *,
        home: Path,
        tz: tzinfo | None,
        bus: EventBus,
        dispatcher: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.tokens = tokens
        self.home = Path(home)
        self.tz = tz
        self.bus = bus
        self.dispatcher = dispatcher
        self.jobs: JobStore | None = None if dispatcher is None else dispatcher.jobs
        self.waiters = AsyncWaiters()
        migrate_jobs_schema(store)
        self.settings = SqliteSettings(store)
        if config.classifier.effective_models:
            record_classifier_mode(self.settings, config.classifier.mode, clock.now())
        self.panel = PanelAuth(tokens.admin, self.settings, config.panel_session_days, clock)
        self.cache = SnapshotCache(
            bus, clock, config.snapshot_ttl_s, lambda now: build_snapshot(config, store, now, tz)
        )
        self.after_calibration: list[Callable[[str], None]] = []  # out-of-schedule counter check
        self.write_lock = threading.Lock()  # observation check-and-write happens as one unit
        self.usage_reader: Any = None  # set by create_app: reads usage sources on a schedule
        self._view_lock = threading.Lock()
        self._view: tuple[Snapshot, dict[str, Any]] | None = None

    def snapshot(self) -> Snapshot:
        """Current snapshot: the dispatcher has its own (with pool closures), else from cache."""
        if self.dispatcher is not None:
            return self.dispatcher.snapshot()
        return self.cache.get()

    def snapshot_view(self) -> dict[str, Any]:
        """Snapshot as JSON; while the snapshot object is unchanged (no events), the store
        is not read.

        The counter and gateway spend change only together with a bus event, and an event
        rebuilds the snapshot. Usage read state changes only together with a bus event
        too (a read publishes usage.read or usage.read_failed), so it is safe to gather
        here. The returned dict is shared: do not modify it.
        """
        snapshot = self.snapshot()
        with self._view_lock:
            if self._view is None or self._view[0] is not snapshot:
                states = None
                if self.usage_reader is not None:
                    states = {
                        pool.id: self.usage_reader.state(pool.id) for pool in self.config.pools
                    }
                self._view = (
                    snapshot,
                    snapshot_json(snapshot, self.store, self.jobs, self.config, states),
                )
            return self._view[1]

    def views(self) -> list[RouteView]:
        if self.dispatcher is not None:
            return self.dispatcher.views()
        return route_views(self.config)

    def advise(
        self,
        tier: Tier,
        caller: str | None,
        mode: Mode | None = None,
        exclude_pools: frozenset[str] = frozenset(),
        reviewed_route: str | None = None,
    ) -> Decision:
        return advise(
            self.config,
            self.snapshot(),
            tier,
            caller,
            mode,
            self.views(),
            exclude_pools,
            reviewed_route,
        )

    def pool(self, pool_id: str) -> Pool | None:
        return next((pool for pool in self.config.pools if pool.id == pool_id), None)

    def record_observation(self, obs: Observation) -> bool:
        """Writes an observation, then publishes an event; True means a new row was added."""
        added = self.store.add_observation(obs)
        self.bus.publish(
            "observation.added" if added else "observation.updated",
            {
                "pool": obs.pool,
                "window": obs.window,
                "source": obs.source,
                "fraction": obs.fraction,
            },
        )
        return added


class AsyncWaiters:
    """Bus-to-asyncio bridge for waiting on a job (GET /api/v1/jobs/{id}/wait).

    Only the event loop thread touches the wait registry: notify from the bus thread
    merely schedules the wakeup via call_soon_threadsafe, so no lock is needed.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: dict[str, set[asyncio.Event]] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def notify(self, event: Event) -> None:
        """Bus subscriber: a job.* event wakes everyone waiting on that job."""
        if not event.type.startswith("job."):
            return
        job_id = event.data.get("job")
        loop = self._loop
        if loop is None or not isinstance(job_id, str):
            return
        with contextlib.suppress(RuntimeError):  # event loop already closed
            loop.call_soon_threadsafe(self._wake, job_id)

    def _wake(self, job_id: str) -> None:
        for changed in self._waiting.get(job_id, ()):
            changed.set()

    @contextlib.contextmanager
    def watch(self, job_id: str) -> Iterator[asyncio.Event]:
        """Registers the wait before the job is read; unregisters on exit, incl. on cancel."""
        changed = asyncio.Event()
        group = self._waiting.setdefault(job_id, set())
        group.add(changed)
        try:
            yield changed
        finally:
            group.discard(changed)
            if not group and self._waiting.get(job_id) is group:
                del self._waiting[job_id]

    def watching(self) -> int:
        return sum(len(group) for group in self._waiting.values())


def build_dispatcher(
    config: Config,
    store: Store,
    jobs: JobStore,
    bus: EventBus,
    clock: Clock,
    home: Path,
    tz: tzinfo | None,
) -> Dispatcher:
    """Dispatcher with a real runner and adapters, plus a thread pool for max_parallel attempts."""
    # dispatcher imports service: importing inside the function breaks the import cycle
    from agent_router.dispatcher import Dispatcher

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.tasks.max_parallel, thread_name_prefix="attempt"
    )
    votes = None
    if any(model.live for model in config.classifier.effective_models):
        from agent_router.classifier import VoteQueue

        flags = SqliteSettings(store) if isinstance(store, SqliteStore) else None
        votes = VoteQueue(config.classifier, now=clock.now, flags=flags)
    return Dispatcher(
        config,
        store,
        jobs,
        bus,
        clock,
        LocalRunner(),
        registry.harnesses(config),
        executor,
        Path(home),
        tz,
        votes=votes,
    )


def worker_loop(
    dispatcher: Dispatcher, bus: EventBus, stop: threading.Event, interval: float = 0.5
) -> None:
    """Worker loop: one dispatcher step, then sleep until any bus event, at most interval."""
    while not stop.is_set():
        seen = bus.seq  # seq before the step: an event arriving during the step is not lost
        try:
            dispatcher.tick()
        except Exception:
            log.exception("dispatcher step failed")
            stop.wait(interval)  # no bus reliance: a failed step may have published events
            continue
        bus.wait(seen, interval)
