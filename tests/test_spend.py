"""`spend` journal: attempt rows, window summing, price estimates, the "via gateway" row."""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_router import worktree
from agent_router.config import Pool, SpendSource, Window
from agent_router.jobs import Attempt, Job, JobRequest
from agent_router.ledger import window_state
from agent_router.periods import parse_duration
from agent_router.prices import estimate, load_prices
from agent_router.registry import HarnessSpec
from agent_router.service import build_snapshot, snapshot_json
from agent_router.spend import SpendEntry, SpendTotal, entry_for_attempt
from agent_router.store import CounterSample, Observation
from helpers import full_config_text, write_config
from test_dispatcher import ROUTES, ImmediateExecutor, Rig, ScriptAdapter, Step

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TOL = timedelta(minutes=15)
US = timedelta(microseconds=1)
PRICES = '"glm-5.3" = [1.0, 4.0]\n'

# --- test doubles ---------------------------------------------------------------------


class MeteredAdapter(ScriptAdapter):
    """Like ScriptAdapter, but with fixed tokens and cost in every parsed outcome."""

    def __init__(self, cost: float | None = None, tokens=(10, 5)) -> None:
        self.cost = cost
        self.tokens = tokens

    def parse(self, stdout, stderr, exit_code):
        outcome = super().parse(stdout, stderr, exit_code)
        tokens_in, tokens_out = self.tokens
        return dataclasses.replace(
            outcome, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=self.cost
        )


def spend_config(root: Path, backend: str, counter: bool, model: str = "model-x") -> str:
    counter_lines = (
        'counter = "opencode_stats"\ncounter_provider = "opencode-go"\n' if counter else ""
    )
    return f"""
[tasks]
allowed_roots = ['{root.as_posix()}']
max_parallel = 3
max_queued = 200
max_attempts = 1

[[pools]]
id = "pool-s"
title = "pool-s"
unit = "usd"
{counter_lines}
  [[pools.windows]]
  id = "week"
  duration = "7d"
  kind = "fixed"
  role = "pace"
  limit_usd = 10

[[routes]]
id = "s/main"
pool = "pool-s"
backend = "{backend}"
model = "{model}"
tiers = ["standard"]
quality = 5
modes = ["read", "write"]
confirm = false
enabled = true
"""


class SpendRig(Rig):
    """Rig with a single route `s/main` in pool `pool-s` (usd, week window, 10 USD limit)."""

    def __init__(self, *args, specs, text_of, **kwargs) -> None:
        self.specs = specs
        super().__init__(*args, **kwargs)
        self.config = write_config(self.home, text_of(self.root))
        self.dispatcher = self._dispatcher()

    def _dispatcher(self):
        dispatcher = super()._dispatcher()
        dispatcher.harnesses = dict(self.specs)
        return dispatcher

    def rows(self) -> list[dict]:
        cursor = self.jobs._store.connection().execute(
            "SELECT pool, source, route, tokens_in, tokens_out, cost_usd, estimated, ref_id, ref_n"
            " FROM spend ORDER BY ref_id, ref_n"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def calibrate(self, fraction: float, at: datetime) -> None:
        self.store.add_observation(
            Observation("pool-s", "week", fraction, at, at + timedelta(days=3), "manual")
        )

    def used(self) -> float | None:
        return self.dispatcher.snapshot().pools["pool-s"].windows[0].state.used


def new_spend_rig(
    tmp_path,
    home,
    store,
    jobstore,
    clock,
    register_harness,
    adapter=None,
    *,
    backend: str = "fake",
    spend: SpendSource = "journal",
    counter: bool = False,
    model: str = "model-x",
) -> SpendRig:
    """SpendRig with a fake `backend` harness (via register_harness); close with rig.close()."""
    register_harness("fake", HarnessSpec("fake", ScriptAdapter(), ("fake",), "journal"))
    spec = HarnessSpec(backend, adapter or MeteredAdapter(), (backend,), spend)
    register_harness(backend, spec)
    return SpendRig(
        tmp_path,
        home,
        store,
        jobstore,
        clock,
        ImmediateExecutor(),
        ROUTES,
        specs={backend: spec},
        text_of=lambda root: spend_config(root, backend, counter, model),
    )


@pytest.fixture
def spend_rig(tmp_path, home, store, jobstore, clock, register_harness):
    rigs: list[SpendRig] = []

    def build(adapter=None, **fields) -> SpendRig:
        rig = new_spend_rig(
            tmp_path, home, store, jobstore, clock, register_harness, adapter, **fields
        )
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


def orphan_job(rig: SpendRig, route: str = "s/main") -> Job:
    """A running job with an unfinished `journal` attempt, as after a gateway crash."""
    cwd = rig.plain()
    job = Job(
        id="j_orphan",
        request=JobRequest("job", cwd, "read"),  # type: ignore[arg-type]
        status="running",
        tier="standard",
        tier_source="default",
        repo_root=worktree.repo_root(cwd),
        route=route,
        created_at=rig.clock.now(),
    )
    rig.jobs.create(job)
    rig.jobs.add_attempt(
        Attempt(job.id, 1, route, "pool-s", rig.clock.now(), spend_source="journal")
    )
    return job


# --- journal rows from attempts -------------------------------------------------------


def test_journal_route_writes_one_spend_row(spend_rig) -> None:
    rig = spend_rig(MeteredAdapter(cost=0.12))
    rig.calibrate(0.2, rig.clock.now() - timedelta(hours=1))
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    assert rig.attempts(job)[0].spend_source == "journal"
    assert rig.rows() == [
        {
            "pool": "pool-s",
            "source": "job",
            "route": "s/main",
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": pytest.approx(0.12),
            "estimated": 0,
            "ref_id": job.id,
            "ref_n": 1,
        }
    ]
    (event,) = rig.bus.data("spend.recorded")
    assert event == {
        "pool": "pool-s",
        "source": "job",
        "route": "s/main",
        "cost_usd": pytest.approx(0.12),
        "tokens_in": 10,
        "tokens_out": 5,
        "estimated": False,
    }
    assert rig.used() == pytest.approx(0.2 + 0.012)  # dispatcher adds journal rows to the window


def test_counter_route_writes_no_spend(spend_rig) -> None:
    rig = spend_rig(MeteredAdapter(cost=0.12), backend="opencode", spend="counter", counter=True)
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    assert rig.attempts(job)[0].spend_source == "counter"
    assert rig.rows() == []
    assert rig.bus.data("spend.recorded") == []


@pytest.mark.parametrize(
    ("step", "outcome"),
    [
        (Step("ok:done"), "ok"),
        (Step("limit"), "limit"),
        (Step("error:failure"), "error"),
        (Step("", exit_code=None, timed_out=True), "timeout"),
        (Step(cancels=True), "cancelled"),
    ],
    ids=["ok", "limit", "error", "timeout", "cancelled"],
)
def test_every_outcome_writes_spend(spend_rig, step: Step, outcome: str) -> None:
    rig = spend_rig()
    rig.runner.add(step)
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == outcome
    rows = rig.rows()
    assert [(row["ref_id"], row["ref_n"]) for row in rows] == [(job.id, 1)]


def test_recover_twice_no_duplicate(spend_rig) -> None:
    rig = spend_rig()
    job = orphan_job(rig)
    rig.dispatcher.recover()
    rig.restart().recover()
    rows = rig.rows()
    assert [(row["ref_id"], row["ref_n"], row["cost_usd"]) for row in rows] == [(job.id, 1, None)]
    assert len(rig.bus.data("spend.recorded")) == 1


def test_recover_after_route_removed(spend_rig) -> None:
    rig = spend_rig()
    job = orphan_job(rig, route="gone/route")  # route absent from config: source from the record
    rig.dispatcher.recover()
    (row,) = rig.rows()
    assert (row["ref_id"], row["route"], row["source"]) == (job.id, "gone/route", "job")
    assert row["cost_usd"] is None


def test_final_write_retry_after_failure(spend_rig, monkeypatch) -> None:
    rig = spend_rig(MeteredAdapter(cost=0.5))
    job = rig.submit(rig.plain())
    real = rig.jobs.update_attempt
    failed: list[int] = []

    def broken(attempt, spend=None):
        if attempt.finished_at is not None and not failed:
            failed.append(attempt.n)
            raise RuntimeError("store unavailable")
        return real(attempt, spend=spend)

    monkeypatch.setattr(rig.jobs, "update_attempt", broken)
    rig.tick(2)
    assert failed == [1]
    (attempt,) = rig.attempts(job)
    assert attempt.finished_at is not None
    assert [(row["ref_id"], row["ref_n"]) for row in rig.rows()] == [(job.id, 1)]


def test_final_write_retry_keeps_measured_spend(spend_rig, monkeypatch) -> None:
    rig = spend_rig(MeteredAdapter(cost=0.5, tokens=(10, 5)))
    job = rig.submit(rig.plain())
    real = rig.jobs.update_attempt
    failed: list[int] = []

    def broken(attempt, spend=None):
        if attempt.finished_at is not None and not failed:
            failed.append(attempt.n)
            raise RuntimeError("store unavailable")
        return real(attempt, spend=spend)

    monkeypatch.setattr(rig.jobs, "update_attempt", broken)
    rig.tick(2)
    assert failed == [1]
    (attempt,) = rig.attempts(job)
    assert (attempt.tokens_in, attempt.tokens_out, attempt.cost_usd) == (10, 5, 0.5)
    assert attempt.duration_s is not None
    (row,) = rig.rows()
    assert (row["tokens_in"], row["tokens_out"], row["cost_usd"]) == (10, 5, 0.5)


# --- store ----------------------------------------------------------------------------


def stored_attempt(jobstore, tmp_path: Path) -> Attempt:
    job = Job(
        id="j_tx",
        request=JobRequest("job", tmp_path, "read"),  # type: ignore[arg-type]
        status="running",
        tier="standard",
        tier_source="default",
        repo_root=None,
        route="s/main",
        created_at=NOW,
    )
    jobstore.create(job)
    attempt = Attempt(job.id, 1, "s/main", "pool-s", NOW, spend_source="journal")
    jobstore.add_attempt(attempt)
    return attempt


def entry(at: datetime, cost: float | None, *, ref: str = "j_1", n: int = 1, **fields):
    values = {
        "pool": "p",
        "at": at,
        "source": "job",
        "route": "r",
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": cost,
        "estimated": False,
        "prices_version": None,
        "ref_id": ref,
        "ref_n": n,
        **fields,
    }
    return SpendEntry(**values)


def test_update_attempt_same_transaction(jobstore, tmp_path, monkeypatch) -> None:
    attempt = stored_attempt(jobstore, tmp_path)
    attempt.finished_at = NOW + timedelta(minutes=1)
    attempt.outcome = "ok"
    row = entry(attempt.finished_at, 0.3, ref=attempt.job_id, pool="pool-s")
    real = jobstore._store.transaction

    class FailingSpendInsert:
        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, *args):
            if " ".join(sql.split()).upper().startswith("INSERT INTO SPEND"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def transaction():
        with real() as conn:
            yield FailingSpendInsert(conn)

    monkeypatch.setattr(jobstore._store, "transaction", transaction)
    with pytest.raises(sqlite3.OperationalError):
        jobstore.update_attempt(attempt, spend=row)
    monkeypatch.undo()
    (kept,) = jobstore.attempts(attempt.job_id)
    assert kept.finished_at is None and kept.outcome is None  # the attempt row rolled back
    assert jobstore.spend_since("pool-s", None).jobs == 0
    assert jobstore.update_attempt(attempt, spend=row) is True
    assert jobstore.update_attempt(attempt, spend=row) is False  # same ref: no duplicate
    assert jobstore.spend_since("pool-s", None).cost_usd == pytest.approx(0.3)


# --- summing within a window ----------------------------------------------------------


def win(kind: str = "fixed") -> Window:
    return Window("week", parse_duration("7d"), kind, "pace", None, 10.0, 1.0)


def pool_of(window: Window, unit: str = "usd", counter: str | None = None) -> Pool:
    return Pool("p", "Pool", unit, (window,), 0.0, None, counter, None, None)


def observe(store, fraction: float, at: datetime, resets: datetime) -> None:
    store.add_observation(Observation("p", "week", fraction, at, resets, "manual"))


def test_spend_boundary_strict(store, jobstore) -> None:
    w = win()
    base = NOW - timedelta(hours=2)
    observe(store, 0.2, base, NOW + timedelta(days=2))
    for n, at in enumerate((base - US, base, base + US), start=1):
        assert jobstore.add_spend(entry(at, 1.0, n=n)) is True
    state = window_state(pool_of(w), w, store, NOW, TOL, spend=jobstore)
    assert state.used == pytest.approx(0.3)
    long_one = Attempt(
        "j_long",
        1,
        "r",
        "p",
        base - timedelta(hours=1),
        finished_at=base + timedelta(minutes=1),
        outcome="ok",
        cost_usd=1.0,
        spend_source="journal",
    )
    spent = entry_for_attempt(long_one)
    assert spent.at == base + timedelta(minutes=1)  # timestamp is the attempt end
    assert jobstore.add_spend(spent) is True
    state = window_state(pool_of(w), w, store, NOW, TOL, spend=jobstore)
    assert state.used == pytest.approx(0.4)


def test_spend_after_scheduled_reset_counts_from_window_start(store, jobstore) -> None:
    w = win()
    reset = NOW - timedelta(hours=3)
    observe(store, 0.8, NOW - timedelta(hours=20), reset)
    jobstore.add_spend(entry(reset - timedelta(minutes=1), 1.0, n=1))
    jobstore.add_spend(entry(reset + timedelta(hours=1), 2.0, n=2))
    state = window_state(pool_of(w), w, store, NOW, TOL, spend=jobstore)
    assert state.source == "scheduled_reset"
    assert state.used == pytest.approx(0.2)


@pytest.mark.parametrize("priced", [False, True])
def test_counter_and_journal_add_independently(store, jobstore, priced: bool) -> None:
    w = win()
    base = NOW - timedelta(hours=2)
    observe(store, 0.0, base, NOW + timedelta(days=2))
    store.add_counter_sample(CounterSample("p", 10.0, base - timedelta(minutes=3)))
    store.add_counter_sample(CounterSample("p", 11.0, NOW))
    jobstore.add_spend(entry(base + timedelta(hours=1), 2.0, estimated=priced))
    pool = pool_of(w, counter="opencode_stats")
    state = window_state(pool, w, store, NOW, TOL, spend=jobstore)
    assert state.used == pytest.approx(0.3)
    assert state.estimated is True
    assert state.estimated_prices is priced


def test_no_base_sample_keeps_journal(store, jobstore) -> None:
    w = win()
    base = NOW - timedelta(hours=2)
    observe(store, 0.4, base, NOW + timedelta(days=2))
    store.add_counter_sample(CounterSample("p", 13.0, NOW))  # past the tolerance from the base
    jobstore.add_spend(entry(base + timedelta(hours=1), 1.0))
    pool = pool_of(w, counter="opencode_stats")
    state = window_state(pool, w, store, NOW, TOL, spend=jobstore)
    assert state.used == pytest.approx(0.5)
    assert state.estimated is False
    assert "counter: no baseline sample" in (state.note or "")


def test_percent_pool_ignores_journal(store, jobstore) -> None:
    w = Window("week", parse_duration("7d"), "fixed", "pace", None, None, 1.0)
    observe(store, 0.4, NOW - timedelta(hours=1), NOW + timedelta(days=2))
    jobstore.add_spend(entry(NOW - timedelta(minutes=30), 5.0, estimated=True))
    state = window_state(pool_of(w, unit="percent"), w, store, NOW, TOL, spend=jobstore)
    assert state.used == pytest.approx(0.4)
    assert state.estimated_prices is False


# --- pricing estimates ----------------------------------------------------------------


def test_price_estimate(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    table = load_prices(path)
    assert table.version == version
    assert estimate(table, "glm-5.3", 100_000, 10_000) == pytest.approx(0.14)
    assert estimate(table, "nope", 100_000, 10_000) is None
    assert estimate(table, "glm-5.3", None, None) is None

    attempt = Attempt(
        "j_p", 1, "r", "p", NOW, finished_at=NOW, tokens_in=100_000, tokens_out=10_000
    )
    # estimate_attempt_cost computes the estimate; entry_for_attempt reads it from the attempt
    priced = dataclasses.replace(attempt, cost_usd=0.14, cost_estimated=True)
    spent = entry_for_attempt(priced, version)
    assert spent.cost_usd == pytest.approx(0.14)
    assert (spent.estimated, spent.prices_version) == (True, version)
    assert (spent.source, spent.ref_id, spent.ref_n, spent.pool) == ("job", "j_p", 1, "p")
    bare = entry_for_attempt(attempt)
    assert (bare.cost_usd, bare.estimated, bare.prices_version) == (None, False, None)
    paid = entry_for_attempt(dataclasses.replace(attempt, cost_usd=0.02))
    assert (paid.cost_usd, paid.estimated, paid.prices_version) == (
        pytest.approx(0.02),
        False,
        None,
    )

    text = full_config_text().replace(
        'counter_provider = "opencode-go"\n',
        'counter_provider = "opencode-go"\nprice_file = "prices.toml"\n',
    )
    config = write_config(home, text)
    pool = next(pool for pool in config.pools if pool.id == "opencode-go")
    assert pool.prices is not None and pool.prices.version == version
    assert pool.prices.models["glm-5.3"] == (1.0, 4.0)
    assert all(other.prices is None for other in config.pools if other.id != "opencode-go")


# --- snapshot -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counter", "priced"),
    [(True, False), (False, True), (True, True)],
    ids=["counter-only", "prices-only", "both"],
)
def test_via_gateway_fields(home, store, jobstore, counter: bool, priced: bool) -> None:
    config = write_config(home, full_config_text())
    base = NOW - timedelta(hours=1)
    store.add_observation(
        Observation("opencode-go", "month", 0.1, base, NOW + timedelta(days=10), "manual")
    )
    if counter:
        store.add_counter_sample(CounterSample("opencode-go", 10.0, base - timedelta(minutes=1)))
        store.add_counter_sample(CounterSample("opencode-go", 13.0, NOW))
    jobstore.add_spend(
        entry(base + timedelta(minutes=10), 0.6, pool="opencode-go", estimated=priced)
    )
    jobstore.add_spend(
        entry(
            base + timedelta(minutes=20),
            0.6,
            pool="opencode-go",
            source="proxy",
            ref="req_1",
            tokens_in=100,
            tokens_out=20,
        )
    )
    snapshot = build_snapshot(config, store, NOW, UTC, spend=jobstore)
    pools = {pool["id"]: pool for pool in snapshot_json(snapshot, store, jobstore, config)["pools"]}
    pool = pools["opencode-go"]
    via = pool["via_gateway"]
    assert via["since"] == base.isoformat()
    assert (via["jobs"], via["minutes"], via["tokens"], via["requests"]) == (0, 0.0, 120, 1)
    assert via["cost_usd"] == pytest.approx(1.2)
    month = next(window for window in pool["windows"] if window["id"] == "month")
    assert month["estimated"] is counter
    assert month["estimated_prices"] is priced
    assert month["used"] == pytest.approx(0.1 + (3 / 60 if counter else 0.0) + 1.2 / 60)
    kimi = pools["kimi-code"]
    assert kimi["via_gateway"]["requests"] == 0 and kimi["via_gateway"]["cost_usd"] == 0.0
    assert all(window["estimated_prices"] is False for window in kimi["windows"])


def test_opencode_job_counted_once(spend_rig) -> None:
    rig = spend_rig(MeteredAdapter(cost=1.5), backend="opencode", spend="counter", counter=True)
    base = rig.clock.now() - timedelta(hours=1)
    rig.calibrate(0.2, base)
    rig.store.add_counter_sample(CounterSample("pool-s", 10.0, base - timedelta(minutes=1)))
    # counter sample already includes the job cost (1.5 USD); the gateway must not add it twice
    rig.store.add_counter_sample(CounterSample("pool-s", 11.5, rig.clock.now()))
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    assert rig.jobs.spend_since("pool-s", None) == SpendTotal(0, 0, 0, 0.0, False)
    assert rig.used() == pytest.approx(0.35)
