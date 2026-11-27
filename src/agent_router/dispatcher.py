"""Job dispatcher: the tick() step, attempts, confirmations, and pool blocks."""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from agent_router import worktree
from agent_router.adapters import with_preamble
from agent_router.classifier import resolve_tier
from agent_router.clock import Clock, format_local
from agent_router.config import Config, Route
from agent_router.dispatcher_recovery import REAP_RETRY_PAUSE_S, RecoverFailed, RecoveryMixin
from agent_router.dispatcher_support import (
    _CWD_CHANGED,
    ERROR_TEXT_MAX,
    LOG_TAIL_CHARS,
    _cwd_matches,
    _measurement_tail,
    _read,
    _repo_key,
    _safe_error,
    _tail,
    _writer_key,
    decision_snapshot,
    reason_with_note,
)
from agent_router.events import EventBus
from agent_router.jobs import (
    TERMINAL,
    Attempt,
    AttemptResult,
    Job,
    JobRequest,
    RequestRejected,
    Status,
    WrongState,
    can_transition,
    new_job_id,
    validate_request_directory,
)
from agent_router.jobstore import JobStore
from agent_router.policy import (
    CALLER_POOL_REASON,
    Choice,
    Decision,
    PoolBlock,
    RouteRequest,
    RouteView,
    Snapshot,
    decide,
)
from agent_router.registry import HarnessSpec, resolve_backend
from agent_router.runner import (
    KILL_WAIT_S,
    QUERY_WAIT_S,
    STOP_BUDGET_S,
    ProcessQueryError,
    ProcessResult,
    ResolvedExecutable,
    Runner,
    ShimUnsafeArgs,
    kill_tree,
    process_image,
    process_started,
    resolve_executable,
)
from agent_router.service import SnapshotCache, build_snapshot, review_targets, route_views
from agent_router.spend import SpendEntry, entry_for_attempt, estimate_attempt_cost
from agent_router.store import Store

LIMIT_REASON = "limit hit"
EXPLICIT_REASON = "route set by caller"
VIEWS_TTL_S = 60.0
ROUTE_ERROR_STREAK = 2
ROUTE_ERROR_COOLDOWN_S = 1800
LOCK_GRACE_S = 1.0
QUEUE_PAGE = 50

log = logging.getLogger(__name__)

_Tokens = tuple[int | None, int | None, float | None]  # input, output, price


@dataclass
class _Active:
    job_id: str
    n: int
    route: Route
    cancel: threading.Event
    repo: str | None  # writer lock key; None for a read-mode job
    future: concurrent.futures.Future[AttemptResult] | None = None


class _WriteFailed(Exception):
    """Attempt outcome measured but not written to the store."""

    def __init__(self, result: AttemptResult, attempt: Attempt | None = None) -> None:
        super().__init__("attempt outcome not recorded")
        self.result = result
        self.attempt = attempt  # measured tokens, cost, and duration for the retry


class Dispatcher(RecoveryMixin):
    """All status transitions happen under one lock; worker threads never change statuses."""

    orphan_wait_s = 5.0  # how long recover() waits for an orphaned process to die
    # worst-case _reap path: two image queries with a pause, creation time, kill and check —
    # or orphan_wait_s plus the final check; plus a 5 s margin
    stop_wait_s = (
        STOP_BUDGET_S
        + max(
            4 * QUERY_WAIT_S + KILL_WAIT_S + REAP_RETRY_PAUSE_S,
            orphan_wait_s + QUERY_WAIT_S,
        )
        + 5.0
    )

    def __init__(
        self,
        config: Config,
        store: Store,
        jobs: JobStore,
        bus: EventBus,
        clock: Clock,
        runner: Runner,
        harnesses: Mapping[str, HarnessSpec],
        executor: concurrent.futures.Executor,
        home: Path,
        tz: tzinfo | None = UTC,
        resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
        kill: Callable[[int], None] = kill_tree,
        image: Callable[[int], str | None] = process_image,
        started: Callable[[int], float | None] = process_started,
        votes: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self.jobs = jobs
        self.bus = bus
        self.clock = clock
        self.runner = runner
        self.harnesses = harnesses
        self.executor = executor
        self.home = Path(home)
        self.tz = tz
        self._resolve = resolve
        self._kill = kill
        self._image = image
        self._started = started
        self.classify_executor = votes
        self._guard_failures: set[str] = set()
        self._lock = threading.RLock()
        self._store_lock = threading.Lock()
        self._sealed = False
        self._active: dict[str, _Active] = {}
        self._active_lock = threading.Lock()
        self._writers: dict[str, str] = {}  # repository → writer job
        self._checked_cwds: dict[str, Path] = {}  # resolved at intake; not persisted
        self._stopped = False
        self._views_lock = threading.Lock()
        self._views: list[RouteView] | None = None
        self._views_at: datetime | None = None
        self._exes: dict[str, ResolvedExecutable] = {}
        # route id → end of its model-unavailable exclusion; not persisted across restarts
        self._model_unavailable: dict[str, datetime] = {}
        self._cache = SnapshotCache(bus, clock, config.snapshot_ttl_s, self._build_snapshot)

    # --- snapshot and routes ---------------------------------------------------------

    def _build_snapshot(self, now: datetime) -> Snapshot:
        return build_snapshot(
            self.config,
            self.store,
            now,
            self.tz,
            self.jobs.active_blocks(now),
            spend=self.jobs,
        )

    def snapshot(self) -> Snapshot:
        return self._cache.get()

    def _unavailable(self, route: Route, exes: dict[str, ResolvedExecutable]) -> str | None:
        backend = route.backend
        if backend is None:
            return "harness not set"
        if backend not in self.harnesses:
            return f"unknown harness: {backend}"
        if backend not in exes:
            exes[backend] = resolve_backend(
                self.config.backend_section(backend), self.harnesses[backend], self._resolve
            )
        if exes[backend].path is None:
            return f"executable not found: {backend}"
        return None

    def _refresh_views(self) -> list[RouteView]:
        """The cached route list; recomputed at most once every 60 s. Caller holds the lock."""
        now = self.clock.now()
        fresh = (
            self._views is not None
            and self._views_at is not None
            and 0 <= (now - self._views_at).total_seconds() < VIEWS_TTL_S
        )
        if not fresh:
            exes: dict[str, ResolvedExecutable] = {}
            availability = {
                route.id: self._unavailable(route, exes) for route in self.config.routes
            }
            self._views = route_views(self.config, availability)
            self._views_at = now
            self._exes = exes
        assert self._views is not None
        return self._views

    def _mark_error_streak(self, view: RouteView, now: datetime) -> RouteView:
        """A route is unavailable while its model-unavailable or error-streak cooldown lasts."""
        if not view.available:
            return view
        excluded_until = self._model_unavailable.get(view.route.id)
        if excluded_until is not None and excluded_until > now:
            # one model-unavailable report takes the route out of rotation at once
            reason = f"model unavailable, retry after {format_local(excluded_until, self.tz)}"
            return replace(view, available=False, unavailable_reason=reason)
        count, last = self.jobs.route_errors_in_row(view.route.id)
        if last is None or count < ROUTE_ERROR_STREAK:
            return view
        until = last + timedelta(seconds=ROUTE_ERROR_COOLDOWN_S)
        if until <= now:
            return view
        reason = f"{count} errors in a row, retry after {format_local(until, self.tz)}"
        return replace(view, available=False, unavailable_reason=reason)

    def views(self) -> list[RouteView]:
        """Routes with executable availability and error-streak/model health; the cached list is
        recomputed at most once every 60 s, then marked on every call."""
        with self._views_lock:
            now = self.clock.now()
            return [self._mark_error_streak(view, now) for view in self._refresh_views()]

    def _unmarked_views(self) -> list[RouteView]:
        """The cached routes without the error-streak marking: for a caller-pinned route."""
        with self._views_lock:
            return list(self._refresh_views())

    # --- status transitions ----------------------------------------------------------

    def _pool_of(self, route_id: str | None) -> str | None:
        if route_id is None:
            return None
        return next((r.pool for r in self.config.routes if r.id == route_id), None)

    def _move(self, job: Job, status: Status, *, attempt: Attempt | None = None) -> None:
        """The only place a status changes: write to the store, then the event."""
        before = job.status
        if not can_transition(before, status):
            raise WrongState(f"transition {before} → {status} not allowed")
        job.status = status
        if status in TERMINAL:
            job.finished_at = self.clock.now()
        if attempt is None:
            self.jobs.update(job)
        else:
            self.jobs.start_attempt(job, attempt)
        if status in TERMINAL:
            self._checked_cwds.pop(job.id, None)
        self._announce(job, before)

    def _announce(self, job: Job, before: Status | None) -> None:
        self.bus.publish(
            f"job.{job.status}",
            {
                "job": job.id,
                "from": before,
                "route": job.route,
                "pool": self._pool_of(job.route),
                "error_code": job.error_code,
            },
        )

    def _fail(self, job: Job, code: str, message: str) -> None:
        job.error_code = code
        job.error_message = message
        self._move(job, "failed")

    def _must_get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    # --- calls from request threads --------------------------------------------------

    def submit(self, req: JobRequest) -> Job:
        # filesystem and git checks run outside the lock
        checked = validate_request_directory(req, self.config)
        if req.review_of is not None and self.jobs.get(req.review_of) is None:
            raise RequestRejected("bad_review_of", f"unknown job: {req.review_of}")
        tier = req.tier or "standard"
        pending = (
            self.config.classifier.mode == "active"
            and req.tier is None
            and self.classify_executor is not None
        )
        job = Job(
            id=new_job_id(),
            request=req,
            status="queued",
            tier=tier,
            tier_source="caller" if req.tier is not None else "default",
            repo_root=checked.repo_root,
            created_at=self.clock.now(),
            prompt_sha256=hashlib.sha256(req.prompt.encode("utf-8")).hexdigest(),
            prompt_chars=len(req.prompt),
            tier_pending=pending,
        )
        with self._lock:
            if self.jobs.count_waiting() >= self.config.tasks.max_queued:
                raise RequestRejected("queue_full", "queue is full, try again later")
            self.jobs.create(job)
            self._checked_cwds[job.id] = checked.cwd
            self._announce(job, None)
            if self.classify_executor is not None and not self._stopped:
                # a classifier queue failure must not reject the job
                with contextlib.suppress(Exception):
                    self.classify_executor.submit(job.id, req.prompt, job.created_at)
        return job

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._must_get(job_id)
            if job.status in TERMINAL:
                raise WrongState(f"job already finished: {job.status}")
            if job.status == "running":
                job.cancel_requested = True
                self.jobs.update(job)
                active = next((a for a in self._active_snapshot() if a.job_id == job.id), None)
                if active is not None:
                    active.cancel.set()  # the runner stops the tree; tick() changes the status
                else:
                    self._move(job, "cancelled")  # no executor left: nothing to wait for
                return job
            job.approval_route = None
            job.approval_deadline = None
            self._move(job, "cancelled")
            return job

    def approve(self, job_id: str) -> Job:
        with self._lock:
            job = self._awaiting(job_id)
            job.approved_route = job.approval_route
            job.approval_route = None
            job.approval_deadline = None
            self._move(job, "queued")
            return job

    def deny(self, job_id: str) -> Job:
        with self._lock:
            job = self._awaiting(job_id)
            self._refuse(job)
            return job

    def _awaiting(self, job_id: str) -> Job:
        job = self._must_get(job_id)
        if job.status != "awaiting_approval":
            if job.status == "declined" and job.error_code == "approval_expired":
                raise WrongState("approval deadline expired")
            raise WrongState(f"job is not awaiting approval: {job.status}")
        if job.approval_deadline is not None and job.approval_deadline <= self.clock.now():
            self._expire(job)  # the decision came too late: record the expiry under the lock
            raise WrongState("approval deadline expired")
        return job

    def _expire(self, job: Job) -> None:
        """An approval deadline passed with no decision: the job is declined, not requeued."""
        route = job.approval_route
        timeout = self.config.tasks.approval_timeout_s
        job.error_code = "approval_expired"
        job.error_message = (
            f"approval deadline expired: route {route} was not approved within {timeout} s"
        )
        job.approval_route = None
        job.approval_deadline = None
        self._move(job, "declined")

    def _refuse(self, job: Job) -> None:
        """Denial: the human refused this route, so it is excluded and the job requeues."""
        if job.approval_route is not None:
            job.excluded_routes = job.excluded_routes | {job.approval_route}
        job.approval_route = None
        job.approval_deadline = None
        job.had_refusal = True
        self._move(job, "queued")

    def unblock(self, pool: str) -> None:
        with self._lock:
            had = pool in self.jobs.active_blocks(self.clock.now())
            self.jobs.clear_block(pool)
            if had:
                self.bus.publish("pool.unblocked", {"pool": pool, "reason": "manual"})

    # --- tick ------------------------------------------------------------------------

    def _guard(self, key: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except Exception:
            if key not in self._guard_failures:
                self._guard_failures.add(key)
                log.exception("dispatcher action failed: %s", key)
            return False
        self._guard_failures.discard(key)
        return True

    def tick(self) -> None:
        if self._sealed:
            return
        with self._lock:
            if self._sealed:
                return
            self._collect_classified()
            self._collect()
            now = self.clock.now()
            for pool in self.jobs.expire_blocks(now):
                self.bus.publish("pool.unblocked", {"pool": pool, "reason": "expired"})
            for job in self.jobs.list("awaiting_approval", limit=self.config.tasks.max_queued):
                if job.approval_deadline is not None and job.approval_deadline <= now:
                    self._guard(f"approval:{job.id}", lambda job=job: self._expire(job))
            if not self._stopped:
                self._start_queued()

    def _collect_classified(self) -> None:
        """Votes from the queue: event log entry, event; the primary vote goes into the job."""
        votes = self.classify_executor
        if votes is None:
            return
        try:
            drained = votes.drain()
        except Exception:
            log.exception("vote queue unavailable")
            return
        for job_id, vote in drained:
            try:
                self.jobs.add_vote(job_id, vote, self.clock.now())
            except Exception:
                log.exception("classifier vote not recorded: %s", job_id)
                continue
            self.bus.publish(
                "classifier.voted",
                {
                    "job_id": job_id,
                    "classifier_id": vote.classifier_id,
                    "tier": vote.tier,
                    "confidence": vote.confidence,
                    "latency_ms": vote.latency_ms,
                    "error": vote.error,
                },
            )
            if vote.classifier_id != votes.primary:
                continue
            try:
                job = self.jobs.get(job_id)
                if job is None:
                    continue
                if job.tier_pending and job.status == "queued":
                    # primary decides the tier right away, even on a vote error — no deadline wait
                    tier, source = resolve_tier(vote, job.request.mode, self.config.classifier)
                    job.tier = tier
                    job.tier_source = source
                    job.tier_pending = False
                    if vote.tier is not None:
                        job.classifier_tier = vote.tier
                        job.classifier_confidence = vote.confidence
                elif vote.tier is not None:
                    # shadow or late vote: the tier does not change
                    job.classifier_tier = vote.tier
                    job.classifier_confidence = vote.confidence
                else:
                    continue
                self.jobs.update(job)
            except Exception:
                log.exception("classifier outcome not recorded: %s", job_id)

    def _active_snapshot(self) -> list[_Active]:
        with self._active_lock:
            return list(self._active.values())

    def _free_slots(self) -> int:
        return self.config.tasks.max_parallel - len(self._active_snapshot())

    def _start_queued(self) -> None:
        if self._stopped or self._free_slots() <= 0:
            return  # stopped or no free slots: the queue is not read at all
        after: str | None = None
        while not self._stopped and self._free_slots() > 0:
            page = self.jobs.queued(QUEUE_PAGE, after=after)
            for job in page:
                after = job.id
                if self._stopped or self._free_slots() <= 0:
                    return
                self._guard(f"consider:{job.id}", lambda job=job: self._consider(job))
            if len(page) < QUEUE_PAGE:
                return

    def _consider(self, job: Job) -> None:
        """One queued job: writer lock → directory → attempts → route → start."""
        if self._stopped:
            return
        if job.tier_pending:
            # tier not decided yet: wait for the primary vote, but no longer than wait_s
            wait_s = self.config.classifier.wait_s
            if wait_s is None:
                primary = self.config.classifier.primary_model
                wait_s = (primary.timeout_s if primary is not None else 0.0) + 0.5
            if self.clock.now() - job.created_at < timedelta(seconds=wait_s):
                return
            tier, source = resolve_tier(None, job.request.mode, self.config.classifier)
            job.tier = tier
            job.tier_source = source
            job.tier_pending = False
            self.jobs.update(job)
        write = job.request.mode == "write"
        if write and _writer_key(job.repo_root) in self._writers:
            return  # repository held by another writer: the job waits without holding up others
        try:
            current = validate_request_directory(job.request, self.config)
        except RequestRejected as exc:
            if not self._stopped:
                self._fail(job, exc.code, exc.message)  # do not touch the job root
            return
        if self._stopped:
            return  # after the shutdown flag, the step must not touch the store
        checked = self._checked_cwds.get(job.id)
        changed = checked is not None and _repo_key(checked) != _repo_key(current.cwd)
        if changed or _repo_key(job.repo_root) != _repo_key(current.repo_root):
            self._fail(job, "cwd_changed", _CWD_CHANGED)
            return
        if checked is None:
            # A restarted dispatcher has no intake path in memory; the stored root still anchors it
            checked = current.cwd
            self._checked_cwds[job.id] = checked
        attempts = self.jobs.attempts(job.id)
        if len(attempts) >= self.config.tasks.max_attempts:
            self._exhausted(job, attempts[-1])
            return
        choice, decision = self._choose(job)
        if choice is None:
            self._no_route(job, decision)
            return
        route = choice.route
        if route.confirm and job.approved_route != route.id:
            job.route = route.id
            job.reason = reason_with_note(choice, decision)
            job.approval_route = route.id
            job.approval_deadline = self.clock.now() + timedelta(
                seconds=self.config.tasks.approval_timeout_s
            )
            self._move(job, "awaiting_approval")
            return
        if self._stopped:
            return  # shutdown arrived mid-check: the job stays queued
        self._start(
            job,
            checked,
            choice,
            len(attempts) + 1,
            _writer_key(job.repo_root) if write else None,
            decision,
        )

    def _review_targets(self, review_of: str | None) -> tuple[str | None, str | None]:
        """Model group and pool of the reviewed job's route; nothing when it has not run yet."""
        reviewed = None if review_of is None else self.jobs.get(review_of)
        route_id = None if reviewed is None else reviewed.route
        return review_targets(self.config, route_id)

    def _decide(self, job: Job, routes: list[RouteView], explicit: bool) -> Decision:
        caller = job.request.caller
        # a pinned or approved route is not overridden by the review exclusion
        review_of = job.request.review_of
        model_group, pool = (None, None) if explicit else self._review_targets(review_of)
        request = RouteRequest(
            tier=job.tier,
            mode=job.request.mode,
            caller_pool=self.config.callers.get(caller) if caller else None,
            excluded_routes=job.excluded_routes,
            excluded_pools=job.excluded_pools,
            request_excluded_pools=job.request.excluded_pools,
            explicit=explicit,
            kind="harness",
            reviewed_model_group=model_group,
            reviewed_pool=pool,
        )
        return decide(request, self.snapshot(), routes)

    def _choose(self, job: Job) -> tuple[Choice | None, Decision]:
        views = self.views()
        if job.approved_route is not None:
            # an approved route is the human's explicit choice: it runs despite an error streak
            pinned = [
                view for view in self._unmarked_views() if view.route.id == job.approved_route
            ]
            decision = self._decide(job, pinned, explicit=True)
            if decision.chosen:
                return decision.chosen[0], decision
            # the pinned route no longer passes checks: exclude it and choose again
            job.excluded_routes = job.excluded_routes | {job.approved_route}
            job.approved_route = None
        if job.request.route is not None:
            # the caller pinned the route: it runs even while its error streak cools down
            only = [view for view in self._unmarked_views() if view.route.id == job.request.route]
            decision = self._decide(job, only, explicit=True)
            if decision.chosen:
                # the caller pinned the route: the reason names that, not the pool pace
                return replace(decision.chosen[0], reason=EXPLICIT_REASON), decision
            return None, decision
        decision = self._decide(job, views, explicit=False)
        return (decision.chosen[0] if decision.chosen else None), decision

    def _no_route(self, job: Job, decision: Decision) -> None:
        if job.had_refusal:
            job.error_code = None
            job.error_message = "no matching routes left"
            self._move(job, "declined")
            return
        reasons = "; ".join(f"{item.route.id}: {item.reason}" for item in decision.excluded)
        message = f"no matching route: {reasons or 'no routes'}"
        pool = self._caller_pool(job, decision)
        if pool is not None:
            message += (
                "; the gateway never sends a job back to the caller's own pool "
                f"{pool}: add a route on another pool or run without --caller"
            )
        self._fail(job, "no_route", message)

    def _caller_pool(self, job: Job, decision: Decision) -> str | None:
        """The caller's pool, but only when a route was excluded for it."""
        if not any(item.reason == CALLER_POOL_REASON for item in decision.excluded):
            return None
        pool = self.config.callers.get(job.request.caller)
        if pool is None:
            pool = next(
                (
                    item.route.pool
                    for item in decision.excluded
                    if item.reason == CALLER_POOL_REASON
                ),
                None,
            )
        return pool

    def _exhausted(self, job: Job, last: Attempt) -> None:
        message = f"attempts exhausted; last: {last.outcome or 'unfinished'}"
        self._fail(job, "attempts_exhausted", message)

    def _timeout_s(self, job: Job, route: Route) -> float:
        if job.request.timeout_s is not None:
            return float(job.request.timeout_s)
        if route.timeout_s is not None:
            return float(route.timeout_s)
        return float(self.config.tasks.default_timeout_s)

    def _idle_timeout_s(self, route: Route) -> float | None:
        """The idle limit for a route: None when disabled or the adapter streams no progress."""
        if self.config.tasks.idle_timeout_s == 0:
            return None
        adapter = self.harnesses[route.backend or ""].adapter
        if not getattr(adapter, "reports_progress", True):
            return None
        return float(self.config.tasks.idle_timeout_s)

    def _start(
        self,
        job: Job,
        cwd: Path,
        choice: Choice,
        n: int,
        repo: str | None,
        decision: Decision,
    ) -> None:
        route = choice.route
        job.route = route.id
        job.reason = reason_with_note(choice, decision)
        job_dir = self.home / "jobs" / job.id
        prompt_file = job_dir / "prompt.md"
        text = with_preamble(job.request.prompt, job.request.mode)
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            message = f"job directory unavailable: {type(exc).__name__}"
            self._fail(job, "executor_error", message)
            return
        if self._stopped:  # signal arrived mid-tick: the job stays queued
            return
        now = self.clock.now()
        attempt = Attempt(
            job.id,
            n,
            route.id,
            route.pool,
            started_at=now,
            spend_source=self.config.route_spend(route),
            decision=decision_snapshot(job, decision, now),
        )
        self._move(job, "running", attempt=attempt)
        active = _Active(job.id, n, route, threading.Event(), repo)
        with self._active_lock:
            self._active[job.id] = active
            if self._stopped:  # shutdown() already took its snapshot: cancel the attempt ourselves
                active.cancel.set()
        if repo is not None:
            self._writers[repo] = job.id
        exe = self._exes.get(route.backend or "") or ResolvedExecutable(None, False)
        arguments = (job, cwd, attempt, route, exe, text, prompt_file, active.cancel)
        try:
            future = self.executor.submit(self._work, *arguments)
        except Exception as exc:  # executor is shut down: the attempt never started
            future = concurrent.futures.Future()
            future.set_exception(exc)
        active.future = future
        future.add_done_callback(
            lambda _done, job_id=job.id, number=n: self.bus.publish(
                "attempt.exited", {"job": job_id, "n": number}
            )
        )

    # --- worker thread: never changes statuses ---------------------------------------

    def _finalize_attempt(self, attempt: Attempt) -> None:
        route = next((item for item in self.config.routes if item.id == attempt.route), None)
        pool = next((item for item in self.config.pools if item.id == attempt.pool), None)
        version: str | None = None
        if route is not None and pool is not None:
            estimated = estimate_attempt_cost(attempt, route, pool)
            if estimated is not None:
                attempt.cost_usd, attempt.cost_estimated = estimated[0], True
                version = estimated[1]
        entry: SpendEntry | None = None
        if attempt.spend_source == "journal":
            entry = entry_for_attempt(attempt, version)
        recorded = self.jobs.update_attempt(attempt, spend=entry)
        if entry is None or not recorded:
            return
        self.bus.publish(
            "spend.recorded",
            {
                "pool": entry.pool,
                "source": entry.source,
                "route": entry.route,
                "cost_usd": entry.cost_usd,
                "tokens_in": entry.tokens_in,
                "tokens_out": entry.tokens_out,
                "estimated": entry.estimated,
            },
        )

    def _write_attempt(self, attempt: Attempt) -> None:
        """After the shutdown flag is set, the worker thread no longer touches the store."""
        with self._store_lock:
            if not self._sealed:
                if attempt.finished_at is None:
                    self.jobs.update_attempt(attempt)
                else:
                    self._finalize_attempt(attempt)

    def _work(
        self,
        job: Job,
        cwd: Path,
        attempt: Attempt,
        route: Route,
        exe: ResolvedExecutable,
        text: str,
        prompt_file: Path,
        cancel: threading.Event,
    ) -> AttemptResult:
        started = time.monotonic()
        duration: float | None = None
        tail = ""
        tokens: _Tokens = (None, None, None)
        try:
            result, duration, tail, tokens = self._attempt(
                job, cwd, attempt, route, exe, text, prompt_file, cancel
            )
        except Exception as exc:  # safety net: failure outside harness launch (store write)
            detail = f"attempt failed: {type(exc).__name__}: {exc}"
            error = _safe_error(detail, cwd)
            result = AttemptResult("error", error=error)
        attempt.finished_at = self.clock.now()
        attempt.outcome = result.outcome
        attempt.duration_s = duration if duration is not None else time.monotonic() - started
        attempt.tokens_in, attempt.tokens_out, attempt.cost_usd = tokens
        attempt.log_tail = tail or (result.error or "")[-LOG_TAIL_CHARS:]
        attempt.result = result
        try:
            self._write_attempt(attempt)  # skipped once the shutdown flag is set
        except Exception as exc:  # the measured outcome goes into the Future with the write error
            raise _WriteFailed(result, attempt) from exc
        return result

    def _attempt(
        self,
        job: Job,
        cwd: Path,
        attempt: Attempt,
        route: Route,
        exe: ResolvedExecutable,
        text: str,
        prompt_file: Path,
        cancel: threading.Event,
    ) -> tuple[AttemptResult, float | None, str, _Tokens]:
        """Before snapshot → harness launch → after measurement, whatever happens at launch."""
        if cancel.is_set():
            return AttemptResult("cancelled"), None, "", (None, None, None)
        in_git = job.repo_root is not None
        if not _cwd_matches(job, cwd):
            failed = AttemptResult("error", error=_CWD_CHANGED, changed=() if in_git else None)
            return failed, None, "", (None, None, None)
        if in_git:
            try:
                head_before = worktree.head(cwd)
                tree_before = worktree.tree_snapshot(cwd)
            except worktree.WorktreeError as exc:
                # without a before snapshot, changes cannot be measured: the harness is not launched
                failed = AttemptResult("error", error=f"working tree snapshot failed: {exc}")
                return failed, None, "", (None, None, None)
            attempt.head_before = head_before
            attempt.tree_before = tree_before
            self._write_attempt(attempt)  # recover() computes changes from the before snapshot
        duration: float | None = None
        tail = ""
        tokens: _Tokens = (None, None, None)
        try:
            result, duration, tail, tokens = self._launch(
                job, attempt, route, exe, text, prompt_file, cancel, cwd
            )
        except ShimUnsafeArgs as exc:
            result = AttemptResult(
                "error",
                error=_safe_error(f"shim: {exc}", cwd),
                survived=attempt.pid is not None and not self._reap(attempt),
            )
        except Exception as exc:
            result = AttemptResult(
                "error",
                error=_safe_error(f"attempt failed: {type(exc).__name__}: {exc}", cwd),
                survived=attempt.pid is not None and not self._reap(attempt),
            )
        if in_git:  # the harness may have changed the tree even on a launch/parsing failure
            if not _cwd_matches(job, cwd):
                result = replace(
                    result,
                    outcome="error",
                    error=_CWD_CHANGED,
                    changed=(),
                    changes_unknown=True,
                )
                tail = _measurement_tail(tail, worktree.WorktreeError(_CWD_CHANGED))
            else:
                try:
                    changed, moved = self._changes(cwd, attempt)
                    result = replace(result, changed=changed, head_moved=moved)
                except Exception as exc:  # measurement failed: changes are unknown, not "none"
                    result = replace(result, changed=(), changes_unknown=True)
                    tail = _measurement_tail(tail, exc)
        return result, duration, tail, tokens

    def _launch(
        self,
        job: Job,
        attempt: Attempt,
        route: Route,
        exe: ResolvedExecutable,
        text: str,
        prompt_file: Path,
        cancel: threading.Event,
        cwd: Path,
    ) -> tuple[AttemptResult, float, str, _Tokens]:
        """Build the command, run the runner, read the logs, and parse the output."""
        adapter = self.harnesses[route.backend or ""].adapter
        invocation = adapter.build(text, job.request.mode, route.model or "", exe, prompt_file)

        def on_start(pid: int) -> None:
            attempt.pid = pid
            try:
                attempt.exe_name = self._image(pid)
            except ProcessQueryError:
                attempt.exe_name = None
            attempt.proc_started = self._started(pid)
            self._write_attempt(attempt)

        done = self.runner.run(
            invocation,
            cwd,
            self._timeout_s(job, route),
            prompt_file.parent / f"attempt-{attempt.n}",
            cancel,
            is_final_line=adapter.is_final_line,
            is_progress_line=getattr(adapter, "is_progress_line", None),
            exit_grace_s=self.config.tasks.exit_grace_s,
            on_start=on_start,
            idle_timeout_s=self._idle_timeout_s(route),
            quiet_while_working=getattr(adapter, "quiet_while_working", False),
        )
        stdout = _read(done.stdout_path)
        stderr = _read(done.stderr_path)
        tail = _tail(stdout, stderr)
        tokens: _Tokens = (None, None, None)
        if done.cancelled:
            result = AttemptResult("cancelled", survived=done.survived)
        elif done.timed_out:
            if done.idle:
                error = f"executor showed no progress for {int(self.config.tasks.idle_timeout_s)} s"
                detail = self._activity_detail(done)
                if detail:
                    error = f"{error}; {detail}"
            else:
                error = "executor exceeded the allotted time"
            result = AttemptResult("timeout", error=error, survived=done.survived)
            # the output up to the timeout may end with the usage line: keep tokens and cost
            try:
                parsed = adapter.parse(stdout, stderr, done.exit_code)
            except Exception:  # truncated or unreadable output: usage stays unknown
                pass
            else:
                tokens = (parsed.tokens_in, parsed.tokens_out, parsed.cost_usd)
                result = replace(result, model_unavailable=parsed.model_unavailable)
        else:
            parsed = adapter.parse(stdout, stderr, done.exit_code)
            tokens = (parsed.tokens_in, parsed.tokens_out, parsed.cost_usd)
            outcome = parsed.status
            error = _safe_error(parsed.error, cwd) if parsed.error is not None else None
            if done.survived and outcome == "ok":
                outcome = "error"
                error = "harness process did not exit after stop"
            result = AttemptResult(
                outcome,
                text=parsed.text,
                error=error,
                limit_reset_at=parsed.limit_reset_at,
                survived=done.survived,
                model_unavailable=parsed.model_unavailable,
            )
        return result, done.duration_s, tail, tokens

    @staticmethod
    def _activity_detail(done: ProcessResult) -> str:
        """What was last seen of the harness; empty when there is nothing to add."""
        if done.last_signal is None or done.last_signal_age_s is None:
            return ""
        return f"last activity: {done.last_signal} {int(done.last_signal_age_s)} s ago"

    @staticmethod
    def _changes(cwd: Path, attempt: Attempt) -> tuple[tuple[str, ...], bool]:
        """Tree changes relative to the before snapshot plus files of new commits if HEAD moved.

        A git failure raises WorktreeError: the caller marks the changes as unknown.
        """
        head_after = worktree.head(cwd)
        files = set(worktree.changed_files(attempt.tree_before or {}, worktree.tree_snapshot(cwd)))
        moved = head_after != attempt.head_before
        if moved and head_after is not None:  # without a before HEAD, take the first commit's files
            files.update(worktree.committed_files(cwd, attempt.head_before, head_after))
        return tuple(sorted(files)), moved

    # --- collecting attempt outcomes ---------------------------------------------------

    def _collect(self) -> None:
        for active in [a for a in self._active_snapshot() if a.future is not None]:
            assert active.future is not None
            if active.future.done():
                self._guard(
                    f"collect:{active.job_id}",
                    lambda active=active: self._finish(active, shutdown=False),
                )

    def _release(self, active: _Active) -> None:
        with self._active_lock:
            self._active.pop(active.job_id, None)
        if active.repo is not None and self._writers.get(active.repo) == active.job_id:
            del self._writers[active.repo]

    def _result_of(self, active: _Active) -> AttemptResult:
        """Outcome from the Future; if the worker thread could not write it, write it here.

        If this write fails too, the exception propagates: the job gets no terminal status
        with an unfinished attempt, and the next tick or recover() collects it again.
        """
        assert active.future is not None
        try:
            return active.future.result(timeout=0)
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc, _WriteFailed) and exc.__cause__ else exc
            job = self.jobs.get(active.job_id)
            cwd = Path(job.request.cwd) if job is not None else Path()
            error = _safe_error(f"attempt failed: {type(cause).__name__}: {cause}", cwd)
            if isinstance(exc, _WriteFailed):  # changes and violations are already measured
                result = replace(exc.result, outcome="error", error=error)
            else:
                result = AttemptResult("error", error=error)
            attempts = self.jobs.attempts(active.job_id)
            last = next((a for a in attempts if a.n == active.n), None)
            if last is not None and last.finished_at is None:
                measured = exc.attempt if isinstance(exc, _WriteFailed) else None
                if measured is not None:  # attempt spend is already parsed — don't lose it
                    last.duration_s = measured.duration_s
                    last.tokens_in = measured.tokens_in
                    last.tokens_out = measured.tokens_out
                    last.cost_usd = measured.cost_usd
                    last.cost_estimated = measured.cost_estimated
                last.finished_at = self.clock.now()
                last.outcome = "error"
                last.log_tail = result.error
                last.result = result
                self._finalize_attempt(last)  # main thread: the shutdown flag does not apply
            return result

    def _finish(self, active: _Active, *, shutdown: bool) -> None:
        result = self._result_of(active)
        job = self.jobs.get(active.job_id)
        # terminal status already written — do not overwrite
        if job is not None and job.status == "running":
            self._apply(job, active.route.pool, result, shutdown=shutdown)
        # The slot and writer lock are released only after the outcome is applied: on a write
        # failure the job stays with the dispatcher, and the next tick applies it again.
        self._release(active)

    def _apply(self, job: Job, pool: str, result: AttemptResult, *, shutdown: bool) -> None:
        write = job.request.mode == "write"
        if result.changed is not None:  # changes from all attempts, not just the last one
            job.changed_files = tuple(sorted({*(job.changed_files or ()), *result.changed}))
        violations = list(job.violations)
        if result.head_moved and "head_moved" not in violations:
            violations.append("head_moved")
        if not write and result.changed and "read_mode_files_changed" not in violations:
            violations.append("read_mode_files_changed")
        if result.changes_unknown and "changes_unknown" not in violations:
            violations.append("changes_unknown")
        if result.survived and "orphan_process" not in violations:
            violations.append("orphan_process")
        job.violations = tuple(violations)
        if result.text:
            job.text = result.text
        # An unmeasured tree and a survived process count as a change: on failure a writer
        # ends up as partial.
        changed = (
            bool(result.changed) or result.head_moved or result.changes_unknown or result.survived
        )
        if result.outcome == "limit":
            self._block(pool, result.limit_reset_at, job.id)
        if (
            result.outcome in {"error", "timeout"}
            and result.model_unavailable
            and job.route is not None
        ):
            cooldown = timedelta(seconds=self.config.tasks.model_unavailable_cooldown_s)
            self._model_unavailable[job.route] = self.clock.now() + cooldown
        if job.cancel_requested or (result.outcome == "cancelled" and not shutdown):
            self._move(job, "cancelled")
        elif result.outcome == "ok":
            job.error_code = None
            job.error_message = None
            self._move(job, "completed")
        elif shutdown:
            self._emergency(job, "gateway_restarted", "gateway stopped during an attempt", changed)
        elif result.outcome == "limit":
            message = result.error or LIMIT_REASON
            if result.survived or (write and changed):
                self._emergency(job, "limit", message, changed)
                return
            job.excluded_pools = job.excluded_pools | {pool}
            attempts = self.jobs.attempts(job.id)
            if len(attempts) >= self.config.tasks.max_attempts:
                self._exhausted(job, attempts[-1])
                return
            job.error_code = None
            job.error_message = None
            self._move(job, "queued")  # next attempt runs in a different pool
        elif result.outcome == "timeout":
            self._emergency(job, "timeout", result.error or "timeout", changed)
        elif not changed and not result.survived and job.request.route is None:
            # executor error with nothing changed and no surviving process: exclude the route
            # and retry elsewhere; a writing attempt with changes ends partial below
            job.excluded_routes = job.excluded_routes | {job.route}
            attempts = self.jobs.attempts(job.id)
            if len(attempts) >= self.config.tasks.max_attempts:
                self._exhausted(job, attempts[-1])
                return
            job.error_code = None
            job.error_message = None
            self._move(job, "queued")  # next attempt runs on another route
        else:
            self._emergency(job, "executor_error", result.error or "executor error", changed)

    def _emergency(self, job: Job, code: str, message: str, changed: bool) -> None:
        """Emergency outcome: partial for a writing attempt with changes, otherwise failed."""
        job.error_code = code
        job.error_message = message
        partial = job.request.mode == "write" and changed
        self._move(job, "partial" if partial else "failed")

    def _block(self, pool: str, reset_at: datetime | None, job_id: str) -> None:
        now = self.clock.now()
        tasks = self.config.tasks
        if reset_at is not None and reset_at > now:
            until = reset_at
        else:
            hits = max(1, self.jobs.limit_hits_in_row(pool))  # the current limit hit is recorded
            seconds = min(tasks.limit_backoff_s * 2 ** (hits - 1), tasks.limit_backoff_max_s)
            until = now + timedelta(seconds=seconds)
        # An active block is never shortened: a parallel limit hit does not cancel an
        # already recognized reset time. The event carries the deadline that actually applies.
        block = self.jobs.add_block(PoolBlock(pool, until, LIMIT_REASON), job_id)
        self.bus.publish("pool.blocked", {"pool": pool, "until": block.until.isoformat()})

    # --- gateway start and shutdown ----------------------------------------------------

    def shutdown(self, wait_s: float | None = None) -> None:
        """Cancel attempts, wait for the overall deadline, and seal worker-thread writes."""
        timeout = self.stop_wait_s if wait_s is None else wait_s
        deadline = time.monotonic() + timeout
        # The stop signal and attempt cancellation do not wait for the lock: a dispatcher
        # tick may hold it for the whole directory check (git has its own deadline).
        with self._active_lock:  # an attempt inserted after this is cancelled by _start itself
            self._stopped = True
        for active in self._active_snapshot():
            active.cancel.set()
        if self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                active = self._active_snapshot()
                for item in active:
                    item.cancel.set()
            finally:
                self._lock.release()
        else:
            active = self._active_snapshot()
            for item in active:
                item.cancel.set()
        futures = [a.future for a in active if a.future is not None]
        concurrent.futures.wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        with self._store_lock:
            self._sealed = True
            # a thread done by now has written its outcome; a late one has not
            done = [f for f in futures if f.done()]
        if self._lock.acquire(timeout=LOCK_GRACE_S):
            try:
                for active in self._active_snapshot():
                    if active.future in done:
                        try:
                            self._finish(active, shutdown=True)
                        except Exception:  # outcome not written: the job stays running
                            log.exception("attempt outcome not applied on shutdown")
                # the ones that did not finish in time stay running until recover()
            finally:
                self._lock.release()
        else:
            log.warning("dispatcher step held the lock: recover() will settle the outcomes")


__all__ = ["ERROR_TEXT_MAX", "LOG_TAIL_CHARS", "Dispatcher", "RecoverFailed", "decision_snapshot"]
