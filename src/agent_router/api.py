"""Gateway HTTP API (prefix /api/v1) and FastAPI application assembly."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from agent_router import __version__
from agent_router.auth import Role, Tokens, host_allowed, role_for
from agent_router.clock import Clock, to_utc
from agent_router.collectors.claude_statusline import (
    accept_statusline,
    observations_from_rate_limits,
)
from agent_router.config import TIERS, Config
from agent_router.events import EventBus
from agent_router.jobs import (
    MODES,
    RATING_VALUES,
    STATUSES,
    TERMINAL,
    Attempt,
    Job,
    JobRequest,
    Rating,
    RequestRejected,
    WrongState,
)
from agent_router.ledger import window_state
from agent_router.periods import parse_span, shift
from agent_router.registry import Counter, load_counter
from agent_router.service import Gateway, decision_json, worker_loop
from agent_router.sinks import EventSink, attach_sinks
from agent_router.store import Observation, Store

STOP_WAIT_S = 2.0
WAIT_MAX_S = 110.0  # max time to wait for a job in a single request
WAIT_SAFETY_S = 1.0  # safety interval for re-reading the job in wait
JOBS_LIST_MAX = 200


class ApiError(Exception):
    """API error: response body is {"error": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


class HostCheck:
    """Rejects requests with a foreign Host header (DNS rebinding protection).

    Pure ASGI instead of ``@app.middleware("http")``: the BaseHTTPMiddleware wrapper
    hides the ``http.disconnect`` message from the handler, so ``wait`` never learned
    that the connection was dropped.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            raw = next((value for name, value in scope["headers"] if name == b"host"), None)
            if not host_allowed(None if raw is None else raw.decode("latin-1")):
                response = error_response(400, "bad_host", "host header not accepted")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def require(gateway: Gateway, role: Role) -> Callable[[Request], Role]:
    """FastAPI dependency: role from the Authorization header; admin works wherever client does."""

    def check(request: Request) -> Role:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        found = role_for(value.strip(), gateway.tokens) if scheme.lower() == "bearer" else None
        if found is None:
            raise ApiError(401, "unauthorized", "missing or invalid token")
        if role == "admin" and found != "admin":
            raise ApiError(403, "forbidden", "admin token required")
        return found

    return check


# how long a calibration response waits for the extra counter sample: opencode stats may run
# for up to its own timeout, while client commands give up after 5 s
AFTER_CALIBRATION_WAIT_S = 2.0


class ObservationBody(BaseModel):
    """Manual calibration body: percent 0..100 and at most one reset-time field."""

    model_config = ConfigDict(extra="forbid")

    pool: str
    window: str
    percent: StrictFloat | StrictInt = Field(ge=0, le=100)
    resets_at: AwareDatetime | None = None
    resets_in: str | None = None


def calibrate(gateway: Gateway, body: ObservationBody) -> dict[str, Any]:
    """Manual calibration: observation with source manual, always accepted."""
    pool = gateway.pool(body.pool)
    if pool is None:
        raise ApiError(422, "unknown_pool", f"unknown pool: {body.pool}")
    window = next((item for item in pool.windows if item.id == body.window), None)
    if window is None:
        raise ApiError(422, "unknown_window", f"pool {pool.id} has no window: {body.window}")
    if body.resets_at is not None and body.resets_in is not None:
        raise ApiError(422, "bad_request", "pass either resets_at or resets_in, not both")
    now = gateway.clock.now()
    resets_at = None if body.resets_at is None else to_utc(body.resets_at)
    if body.resets_in is not None:
        try:
            resets_at = now + parse_span(body.resets_in)
        except ValueError as exc:
            raise ApiError(422, "bad_request", str(exc)) from exc
    if resets_at is not None and resets_at <= now:
        raise ApiError(422, "bad_request", "reset time is already in the past")
    if resets_at is not None and window.kind == "rolling":
        raise ApiError(
            422,
            "bad_request",
            f"window {window.id} is rolling: it has no reset time, calibrate without a reset",
        )
    if (
        resets_at is not None
        and window.kind == "fixed"
        and resets_at > shift(now, window.duration, 1) + timedelta(minutes=5)
    ):
        raise ApiError(422, "bad_request", f"reset is more than one {window.id} window ahead")
    obs = Observation(pool.id, body.window, float(body.percent) / 100.0, now, resets_at, "manual")
    with gateway.write_lock:
        added = gateway.record_observation(obs)
    _after_calibration(gateway, pool.id)
    kept = (
        window_state(pool, window, gateway.store, now).resets_at if window.kind == "fixed" else None
    )
    return {
        "added": added,
        "pool": obs.pool,
        "window": obs.window,
        "fraction": obs.fraction,
        "observed_at": obs.observed_at.isoformat(),
        "resets_at": None if obs.resets_at is None else obs.resets_at.isoformat(),
        "source": obs.source,
        "window_resets_at": None if kept is None else kept.isoformat(),
    }


def _after_calibration(gateway: Gateway, pool_id: str) -> None:
    """Runs the extra counter sampling; a slow counter finishes after the response."""
    hooks = list(gateway.after_calibration)
    if not hooks:
        return

    def run() -> None:
        for hook in hooks:
            with contextlib.suppress(
                Exception
            ):  # a failed extra sampling must not break calibration
                hook(pool_id)

    worker = threading.Thread(target=run, name="after-calibration", daemon=True)
    worker.start()
    worker.join(AFTER_CALIBRATION_WAIT_S)


def accept_signal(gateway: Gateway, payload: Any) -> int:
    """status line rate_limits block → observations; returns how many were accepted."""
    settings = gateway.config.statusline
    if settings is None or not isinstance(payload, Mapping):
        return 0
    pool = gateway.pool(settings.pool)
    if pool is None:
        return 0
    windows = {window.id for window in pool.windows}
    now = gateway.clock.now()
    accepted = 0
    for obs in observations_from_rate_limits(payload.get("rate_limits"), now, settings):
        if obs.window not in windows:
            continue
        with gateway.write_lock:  # staleness check and write happen as one atomic block
            last = gateway.store.last_observation(obs.pool, obs.window)
            if not accept_statusline(obs, last, now):
                continue
            gateway.record_observation(obs)
        accepted += 1
    return accepted


class JobBody(BaseModel):
    """POST /api/v1/jobs body; validate_request checks the values."""

    model_config = ConfigDict(extra="forbid")

    prompt: StrictStr
    cwd: StrictStr
    mode: StrictStr = "read"
    tier: StrictStr | None = None
    kind: StrictStr | None = None
    caller: StrictStr | None = None
    route: StrictStr | None = None
    timeout_s: Any = None  # fractional, boolean, etc. — error code bad_timeout, not bad_request


class RatingBody(BaseModel):
    """POST /api/v1/jobs/{id}/rating body; the note text is not stored."""

    model_config = ConfigDict(extra="forbid")

    value: StrictStr
    should_tier: StrictStr | None = None
    by: StrictStr | None = None
    note: StrictStr | None = None


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else to_utc(moment).isoformat()


def rating_json(rating: Rating) -> dict[str, Any]:
    """Job rating in the JSON response: no note text, only its hash and length."""
    return {
        "job_id": rating.job_id,
        "value": rating.value,
        "should_tier": rating.should_tier,
        "by": rating.by,
        "note_sha256": rating.note_sha256,
        "note_chars": rating.note_chars,
        "at": _iso(rating.at),
    }


def require_dispatcher(gateway: Gateway) -> Any:
    """Job dispatcher, or the 503 tasks_disabled error shared by the jobs routes."""
    if gateway.dispatcher is None:
        raise ApiError(503, "tasks_disabled", "job dispatcher is not running")
    return gateway.dispatcher


def job_json(job: Job, attempts: Sequence[Attempt], rating: Rating | None = None) -> dict[str, Any]:
    """Job state and result; the prompt and tokens are not included."""
    tail = next((a.log_tail for a in reversed(attempts) if a.log_tail), None)
    return {
        "id": job.id,
        "status": job.status,
        "mode": job.request.mode,
        "tier": job.tier,
        "tier_source": job.tier_source,
        "classifier_tier": job.classifier_tier,
        "classifier_confidence": job.classifier_confidence,
        "prompt_sha256": job.prompt_sha256,
        "prompt_chars": job.prompt_chars,
        "kind": job.request.kind,
        "caller": job.request.caller,
        "route": job.route,
        "reason": job.reason,
        "approval": None
        if job.approval_route is None
        else {"route": job.approval_route, "deadline": _iso(job.approval_deadline)},
        "attempts": [
            {
                "n": a.n,
                "route": a.route,
                "pool": a.pool,
                "started_at": _iso(a.started_at),
                "finished_at": _iso(a.finished_at),
                "outcome": a.outcome,
                "duration_s": a.duration_s,
                "tokens_in": a.tokens_in,
                "tokens_out": a.tokens_out,
                "cost_usd": a.cost_usd,
            }
            for a in attempts
        ],
        "text": job.text,
        "changed_files": None if job.changed_files is None else list(job.changed_files),
        "violations": list(job.violations),
        "error": None
        if job.error_code is None
        else {"code": job.error_code, "message": job.error_message or ""},
        "log_tail": tail,
        "created_at": _iso(job.created_at),
        "finished_at": _iso(job.finished_at),
        "rating": None if rating is None else rating_json(rating),
        "cancel_requested": job.cancel_requested,
    }


def rate(gateway: Gateway, job_id: str, body: RatingBody) -> dict[str, Any]:
    """Rate a finished job: validate, store the rating and publish job.rated."""
    dispatcher = require_dispatcher(gateway)
    job = dispatcher.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", f"job not found: {job_id}")
    if job.status not in TERMINAL:
        raise ApiError(409, "job_not_finished", f"job is not finished yet: {job.status}")
    if body.value not in RATING_VALUES:
        raise ApiError(422, "bad_rating", f"unknown rating: {body.value}")
    if body.should_tier is not None and body.should_tier not in TIERS:
        raise ApiError(422, "bad_tier", f"unknown tier: {body.should_tier}")
    rating = Rating(
        job_id=job_id,
        value=body.value,
        should_tier=body.should_tier,
        by=body.by,
        note_sha256=None
        if body.note is None
        else hashlib.sha256(body.note.encode("utf-8")).hexdigest(),
        note_chars=None if body.note is None else len(body.note),
        at=gateway.clock.now(),
    )
    dispatcher.jobs.add_rating(rating)
    gateway.bus.publish(
        "job.rated",
        {"job_id": job_id, "value": rating.value, "should_tier": rating.should_tier},
    )
    return job_json(job, dispatcher.jobs.attempts(job_id), rating)


def _settled(job: Job, now: datetime) -> bool:
    """A job the wait call returns for: finished, or waiting on a human decision."""
    return (
        job.status in TERMINAL
        or job.status == "awaiting_approval"
        or (job.approval_deadline is not None and job.approval_deadline <= now)
    )


class Background:
    """App background threads: each gets a stop event and is joined for at most 2 s."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self, name: str, target: Callable[[threading.Event], None]) -> None:
        thread = threading.Thread(target=target, args=(self.stop,), name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def shutdown(self) -> None:
        self.stop.set()
        for thread in self._threads:
            thread.join(STOP_WAIT_S)
        self._threads.clear()


def api_router(gateway: Gateway) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    client = Depends(require(gateway, "client"))
    admin = Depends(require(gateway, "admin"))

    @router.get("/snapshot")
    def get_snapshot(role: Role = client) -> dict[str, Any]:
        return gateway.snapshot_view()

    @router.post("/observations")
    def post_observation(body: ObservationBody, role: Role = admin) -> dict[str, Any]:
        return calibrate(gateway, body)

    @router.post("/signals/claude-statusline")
    async def post_statusline(request: Request, role: Role = client) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:  # not JSON or not UTF-8: the signal is simply not accepted
            payload = None
        return {"accepted": await run_in_threadpool(accept_signal, gateway, payload)}

    @router.get("/advice")
    def get_advice(
        tier: str = "standard",
        caller: str | None = None,
        mode: str | None = None,
        role: Role = client,
    ) -> dict[str, Any]:
        if tier not in TIERS:
            raise ApiError(422, "bad_request", f"unknown tier: {tier}")
        if mode is not None and mode not in MODES:
            raise ApiError(422, "bad_request", f"unknown mode: {mode}")
        decision = gateway.advise(tier, caller, mode)  # type: ignore[arg-type]
        return decision_json(tier, decision, mode)  # type: ignore[arg-type]

    @router.get("/events/stats")
    def get_event_stats(role: Role = client) -> dict[str, Any]:
        return {"seq": gateway.bus.seq, "subscribers": gateway.bus.stats()}

    # --- jobs ----------------------------------------------------------------------

    def tasks() -> Any:
        return require_dispatcher(gateway)

    def not_found(job_id: str) -> ApiError:
        return ApiError(404, "not_found", f"job not found: {job_id}")

    def act(job_id: str, action: Callable[[str], Job]) -> dict[str, Any]:
        dispatcher = tasks()
        try:
            job = action(job_id)
        except KeyError as exc:
            raise not_found(job_id) from exc
        except WrongState as exc:
            raise ApiError(409, "wrong_state", str(exc)) from exc
        return job_json(job, dispatcher.jobs.attempts(job.id), dispatcher.jobs.last_rating(job.id))

    @router.post("/jobs", status_code=201)
    def post_job(body: JobBody, role: Role = client) -> dict[str, Any]:
        dispatcher = tasks()
        request = JobRequest(
            prompt=body.prompt,
            cwd=Path(body.cwd),
            mode=body.mode,  # type: ignore[arg-type]
            tier=body.tier,  # type: ignore[arg-type]
            kind=body.kind,
            caller=body.caller,
            route=body.route,
            timeout_s=body.timeout_s,
        )
        try:
            job = dispatcher.submit(request)
        except RequestRejected as exc:
            status = 429 if exc.code == "queue_full" else 422
            raise ApiError(status, exc.code, exc.message) from exc
        return {"job_id": job.id, "status": job.status}

    @router.get("/jobs")
    def list_jobs(
        status: str | None = None, limit: int = 50, role: Role = client
    ) -> dict[str, Any]:
        dispatcher = tasks()
        if status is not None and status not in STATUSES:
            raise ApiError(422, "bad_request", f"unknown status: {status}")
        if not 1 <= limit <= JOBS_LIST_MAX:
            raise ApiError(422, "bad_request", f"limit must be between 1 and {JOBS_LIST_MAX}")
        found = dispatcher.jobs.list(status, limit)
        return {
            "jobs": [
                job_json(job, dispatcher.jobs.attempts(job.id), dispatcher.jobs.last_rating(job.id))
                for job in found
            ]
        }

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, role: Role = client) -> dict[str, Any]:
        dispatcher = tasks()
        job = dispatcher.jobs.get(job_id)
        if job is None:
            raise not_found(job_id)
        return job_json(job, dispatcher.jobs.attempts(job_id), dispatcher.jobs.last_rating(job_id))

    @router.get("/jobs/{job_id}/wait")
    async def wait_job(
        job_id: str, request: Request, timeout: float = WAIT_MAX_S, role: Role = client
    ) -> Any:
        """Waits for a terminal status or an approval prompt without holding a server thread.

        An `awaiting_approval` job returns at once: the caller must hand it to a human.
        """
        dispatcher = tasks()
        if not timeout >= 0:  # also rejects NaN
            raise ApiError(422, "bad_request", "timeout must be non-negative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(timeout, WAIT_MAX_S)
        with gateway.waiters.watch(job_id) as changed:
            while True:
                changed.clear()  # before reading: a notification issued after it will not be lost
                job = await run_in_threadpool(dispatcher.jobs.get, job_id)
                if job is None:
                    raise not_found(job_id)
                left = deadline - loop.time()
                if _settled(job, dispatcher.clock.now()) or left <= 0:
                    attempts = await run_in_threadpool(dispatcher.jobs.attempts, job_id)
                    rating = await run_in_threadpool(dispatcher.jobs.last_rating, job_id)
                    return job_json(job, attempts, rating)
                if await request.is_disconnected():
                    # uvicorn does not cancel the handler on disconnect: leave on our own
                    rating = await run_in_threadpool(dispatcher.jobs.last_rating, job_id)
                    return job_json(job, (), rating)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), min(WAIT_SAFETY_S, left))

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, role: Role = client) -> dict[str, Any]:
        return act(job_id, tasks().cancel)

    @router.post("/jobs/{job_id}/approve")
    def approve_job(job_id: str, role: Role = admin) -> dict[str, Any]:
        return act(job_id, tasks().approve)

    @router.post("/jobs/{job_id}/deny")
    def deny_job(job_id: str, role: Role = admin) -> dict[str, Any]:
        return act(job_id, tasks().deny)

    @router.post("/jobs/{job_id}/rating")
    def rate_job(job_id: str, body: RatingBody, role: Role = client) -> dict[str, Any]:
        return rate(gateway, job_id, body)

    @router.post("/pools/{pool_id}/unblock")
    def unblock_pool(pool_id: str, role: Role = admin) -> dict[str, Any]:
        dispatcher = tasks()
        if gateway.pool(pool_id) is None:
            raise ApiError(404, "not_found", f"pool not found: {pool_id}")
        dispatcher.unblock(pool_id)
        # only the manual block is removed; an exhausted window keeps the pool excluded
        status = gateway.snapshot().pools.get(pool_id)
        blocked_by = None if status is None else status.blocked_by
        return {
            "pool": pool_id,
            "blocked": blocked_by is not None,
            "blocked_by": blocked_by,
            "blocked_until": None if blocked_by is None else _iso(status.blocked_until),
        }

    return router


def create_app(
    config: Config,
    store: Store,
    clock: Clock,
    tokens: Tokens,
    *,
    home: Path,
    tz: tzinfo | None = UTC,
    bus: EventBus | None = None,
    dispatcher: Any = None,
    background: bool = True,
) -> FastAPI:
    if bus is None:
        bus = dispatcher.bus if dispatcher is not None else EventBus(clock)
    gateway = Gateway(
        config, store, clock, tokens, home=home, tz=tz, bus=bus, dispatcher=dispatcher
    )
    counter_names = dict.fromkeys(pool.counter for pool in config.pools if pool.counter is not None)
    counters: dict[str, Counter] = {
        name: load_counter(name).factory(config, store, clock, bus) for name in counter_names
    }

    def hook(pool_id: str) -> None:
        pool = gateway.pool(pool_id)
        if pool is not None and pool.counter is not None:
            counters[pool.counter].sample_once()

    gateway.after_calibration.append(hook)
    if dispatcher is not None:
        bus.subscribe("waiters", gateway.waiters.notify)

    def loop(stop: threading.Event) -> None:
        while True:
            for sampler in counters.values():
                with contextlib.suppress(Exception):
                    sampler.sample_once()
            if stop.wait(config.counter_interval_s):
                return

    threads = Background()
    sinks: list[EventSink] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        gateway.waiters.bind(asyncio.get_running_loop())
        try:  # startup also happens inside: resources are released if boot fails
            if background:
                sinks.extend(attach_sinks(bus, config, gateway.home))
                threads.start("counter-sampler", loop)
                if dispatcher is not None:
                    await run_in_threadpool(dispatcher.recover)
                    threads.start("job-worker", lambda stop: worker_loop(dispatcher, bus, stop))
            yield
        finally:
            # shutdown order: worker threads, dispatcher, event bus, sinks, store
            threads.shutdown()
            if dispatcher is not None:
                await run_in_threadpool(dispatcher.shutdown)
                dispatcher.executor.shutdown(wait=False, cancel_futures=True)
                if dispatcher.classify_executor is not None:
                    dispatcher.classify_executor.shutdown(wait=False, cancel_futures=True)
            stuck = set(bus.close())
            for index, sink in enumerate(sinks):
                if f"sink:{config.sinks[index].type}:{index}" not in stuck:
                    with contextlib.suppress(Exception):
                        sink.close()
            sinks.clear()
            if dispatcher is not None:
                dispatcher.jobs.close()
            store.close()

    app = FastAPI(
        title="agent-router",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.gateway = gateway
    app.state.counters = counters
    app.state.counter_sampler = counters.get("opencode_stats")
    app.state.background = threads
    app.state.waiters = gateway.waiters

    app.add_middleware(HostCheck)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        return error_response(422, "bad_request", f"invalid request data: {fields}")

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    app.include_router(api_router(gateway))
    # Local import breaks the cycle: web uses the model and calibration function from api.
    from agent_router.web import web_router

    app.include_router(web_router(gateway))
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    return app
