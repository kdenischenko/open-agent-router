"""Model weight in the Go counter and OpenCode attempt cost estimation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agent_router.collectors.opencode_stats import CounterSampler, counter_from_stats
from agent_router.config import ConfigError, Pool, Window
from agent_router.jobs import Attempt
from agent_router.jobstore import migrate_jobs_schema
from agent_router.ledger import window_state
from agent_router.periods import parse_duration
from agent_router.registry import HarnessSpec
from agent_router.runner import ResolvedExecutable
from agent_router.spend import entry_for_attempt, estimate_attempt_cost
from agent_router.store import CounterSample, Observation
from helpers import full_config_text, write_config
from test_dispatcher import ROUTES, ImmediateExecutor, ScriptAdapter
from test_spend import MeteredAdapter, SpendRig, spend_config

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
TOL = timedelta(minutes=15)
NOTE_SCALE = "model weights changed — recalibrate the pool"
SAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "opencode_stats.json").read_text(encoding="utf-8")
)


def win(limit_usd: float = 30.0) -> Window:
    return Window("week", parse_duration("7d"), "fixed", "pace", None, limit_usd, 1.0)


def weighted_pool(window: Window | None = None, **fields) -> Pool:
    values = {"limit_base_usd": 180.0, "model_limits": {"kimi-k3.1-pro": 60, "longcat-2.0": 240}}
    values.update(fields)
    return Pool(
        "p",
        "Pool",
        "usd",
        (window or win(),),
        0.0,
        None,
        "opencode_stats",
        "opencode-go",
        None,
        **values,
    )


def stats(*entries: tuple[str, float, str | None]) -> dict:
    models = []
    for model_id, cost, variant in entries:
        description = {"id": model_id, "providerID": "opencode-go"}
        if variant is not None:
            description["variant"] = variant
        models.append({"model": description, "steps": 1, "tokens": {}, "cost": cost})
    return {"models": models}


# --- pool: weight and table hash ------------------------------------------------------


def test_pool_scale_by_model_limit() -> None:
    pool = weighted_pool()
    assert pool.scale("kimi-k3.1-pro") == pytest.approx(3.0)
    assert pool.scale("opencode-go/kimi-k3.1-pro#max") == pytest.approx(3.0)
    assert pool.scale("longcat-2.0") == pytest.approx(0.75)
    assert pool.scale("unknown-model") == 1.0


def test_pool_without_base_scale_one_and_no_hash() -> None:
    pool = weighted_pool(limit_base_usd=None, model_limits={})
    assert pool.scale("kimi-k3.1-pro") == 1.0
    assert pool.scale_hash is None
    old = Pool("p", "Pool", "usd", (win(),), 0.0, None, "opencode_stats", "opencode-go", None)
    assert old.scale_hash is None and old.scale("anything") == 1.0
    assert dict(old.model_limits) == {} and old.model_limits_date is None


def test_pool_scale_hash_tracks_table() -> None:
    first = weighted_pool()
    same = weighted_pool(model_limits={"longcat-2.0": 240, "kimi-k3.1-pro": 60})
    other_limit = weighted_pool(model_limits={"kimi-k3.1-pro": 120, "longcat-2.0": 240})
    other_base = weighted_pool(limit_base_usd=90.0)
    assert first.scale_hash is not None and len(first.scale_hash) == 12
    assert first.scale_hash == same.scale_hash  # key order does not matter
    assert len({first.scale_hash, other_limit.scale_hash, other_base.scale_hash}) == 3


def test_pool_model_limits_is_immutable_copy() -> None:
    source = {"kimi-k3.1-pro": 60.0}
    pool = weighted_pool(model_limits=source)
    source["kimi-k3.1-pro"] = 1.0
    assert pool.scale("kimi-k3.1-pro") == pytest.approx(3.0)
    with pytest.raises(TypeError):
        pool.model_limits["longcat-2.0"] = 240  # type: ignore[index]


# --- config ---------------------------------------------------------------------------


def weights_text(lines: str, table: str = '"glm-5.3" = 60\n') -> str:
    extra = f"{lines}\n[pools.model_limits]\n{table}" if table is not None else lines
    return full_config_text().replace(
        'counter_provider = "opencode-go"\n', f'counter_provider = "opencode-go"\n{extra}'
    )


def config_error(home: Path, text: str) -> str:
    with pytest.raises(ConfigError) as caught:
        write_config(home, text)
    return str(caught.value)


def go_pool(config) -> Pool:
    return next(pool for pool in config.pools if pool.id == "opencode-go")


def test_config_reads_model_limits(home: Path) -> None:
    config = write_config(
        home,
        weights_text(
            'limit_base_usd = 180\nmodel_limits_date = "2026-11-16"\n',
            '"glm-5.3" = 120\n"kimi-k3.1-pro" = 60\n',
        ),
    )
    pool = go_pool(config)
    assert pool.limit_base_usd == 180.0
    assert dict(pool.model_limits) == {"glm-5.3": 120.0, "kimi-k3.1-pro": 60.0}
    assert pool.model_limits_date == "2026-11-16"
    assert pool.scale("opencode-go/glm-5.3#high") == pytest.approx(1.5)
    others = [item for item in config.pools if item.id != "opencode-go"]
    assert all(item.limit_base_usd is None and not item.model_limits for item in others)


def test_config_model_limits_date_as_toml_date(home: Path) -> None:
    config = write_config(
        home, weights_text("limit_base_usd = 180\nmodel_limits_date = 2026-11-16\n")
    )
    assert go_pool(config).model_limits_date == date(2026, 11, 16).isoformat()


@pytest.mark.parametrize(
    ("lines", "table", "fragment"),
    [
        ("limit_base_usd = 0\n", '"glm-5.3" = 60\n', "limit_base_usd"),
        ("limit_base_usd = -5\n", '"glm-5.3" = 60\n', "limit_base_usd"),
        ('limit_base_usd = "lots"\n', '"glm-5.3" = 60\n', "limit_base_usd"),
        ("limit_base_usd = 180\n", '"glm-5.3" = 0\n', "model_limits"),
        ("limit_base_usd = 180\n", '"glm-5.3" = -60\n', "model_limits"),
        ("limit_base_usd = 180\n", '"glm-5.3" = true\n', "model_limits"),
        ("", '"glm-5.3" = 60\n', "limit_base_usd"),  # table without a base
        ('limit_base_usd = 180\nmodel_limits_date = "16.11.2026"\n', None, "model_limits_date"),
    ],
    ids=[
        "base-zero",
        "base-negative",
        "base-text",
        "limit-zero",
        "limit-negative",
        "limit-bool",
        "limits-without-base",
        "bad-date",
    ],
)
def test_config_rejects_bad_weights(home: Path, lines: str, table: str | None, fragment: str):
    message = config_error(home, weights_text(lines, table))
    assert fragment in message


# --- counter --------------------------------------------------------------------------


def test_counter_scaled_by_model_limit() -> None:
    pool = weighted_pool()
    data = stats(("kimi-k3.1-pro", 1.0, "high"), ("longcat-2.0", 2.0, None))
    assert counter_from_stats(data, "opencode-go", scale=pool.scale) == pytest.approx(4.5)
    assert counter_from_stats(data, "opencode-go") == pytest.approx(3.0)  # no weight, as before


def test_counter_unknown_model_scale_one() -> None:
    pool = weighted_pool()
    data = stats(("kimi-k3.1-pro", 1.0, "default"), ("glm-5.3-flash", 2.0, "default"))
    assert counter_from_stats(data, "opencode-go", scale=pool.scale) == pytest.approx(5.0)


def test_counter_model_filter_with_scale() -> None:
    pool = weighted_pool()
    data = stats(("kimi-k3.1-pro", 1.0, None), ("longcat-2.0", 2.0, None))
    value = counter_from_stats(data, "opencode-go", "kimi-k3.1-pro", scale=pool.scale)
    assert value == pytest.approx(3.0)


def test_sampler_writes_scaled_value_and_hash(home: Path, store, clock, bus) -> None:
    migrate_jobs_schema(store)
    config = write_config(
        home,
        weights_text("limit_base_usd = 180\n", '"glm-5.3" = 60\n"glm-5.3-flash" = 180\n'),
    )
    sampler = CounterSampler(
        config,
        store,
        clock,
        bus,
        run=lambda executable: SAMPLE,
        resolve=lambda name: ResolvedExecutable(Path(name), False),
    )
    assert sampler.sample_once() == 1
    sample = store.last_counter("opencode-go")
    assert sample is not None
    assert sample.value_usd == pytest.approx(1.25 + 2.75 * 3)
    assert sample.scale_hash == go_pool(config).scale_hash is not None


def test_sampler_without_base_writes_null_hash(config, store, clock, bus) -> None:
    migrate_jobs_schema(store)
    sampler = CounterSampler(
        config,
        store,
        clock,
        bus,
        run=lambda executable: SAMPLE,
        resolve=lambda name: ResolvedExecutable(Path(name), False),
    )
    assert sampler.sample_once() == 1
    sample = store.last_counter("opencode-go")
    assert sample is not None
    assert sample.value_usd == pytest.approx(4.0) and sample.scale_hash is None


# --- observation store ----------------------------------------------------------------


def test_counter_samples_since_inclusive_ordered(store) -> None:
    migrate_jobs_schema(store)
    base = NOW - timedelta(hours=2)
    store.add_counter_sample(CounterSample("p", 1.0, base - timedelta(minutes=1), "a"))
    store.add_counter_sample(CounterSample("p", 3.0, base + timedelta(minutes=5), "b"))
    store.add_counter_sample(CounterSample("p", 2.0, base, "a"))
    store.add_counter_sample(CounterSample("q", 9.0, base + timedelta(minutes=1), None))
    samples = store.counter_samples_since("p", base)
    assert [(s.value_usd, s.scale_hash) for s in samples] == [(2.0, "a"), (3.0, "b")]
    assert store.counter_at("p", base, TOL).scale_hash == "a"
    assert store.last_counter("p").scale_hash == "b"


def test_old_schema_store_reads_null_hash(store) -> None:
    """A store without migration 9 (observation schema only) works as before."""
    store.add_counter_sample(CounterSample("p", 1.0, NOW))
    assert store.last_counter("p").scale_hash is None
    assert [s.value_usd for s in store.counter_samples_since("p", NOW)] == [1.0]
    with pytest.raises(RuntimeError):
        store.add_counter_sample(CounterSample("p", 2.0, NOW, "abc"))


# --- window log: weight change --------------------------------------------------------


def observe(store, window: Window, fraction: float, at: datetime) -> None:
    store.add_observation(
        Observation("p", window.id, fraction, at, NOW + timedelta(days=1), "manual")
    )


def add(store, value: float, at: datetime, scale_hash: str | None) -> None:
    store.add_counter_sample(CounterSample("p", value, at, scale_hash))


def test_scale_change_rebases(store) -> None:
    migrate_jobs_schema(store)
    w = win(30.0)
    base = NOW - timedelta(hours=4)
    observe(store, w, 0.40, base)
    add(store, 10.0, base - timedelta(minutes=3), "old")
    add(store, 12.0, base + timedelta(hours=1), "old")  # +2
    state = window_state(weighted_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40 + 2 / 30)
    assert state.note is None

    add(store, 30.0, base + timedelta(hours=2), "new")  # weight change: the jump is not counted
    state = window_state(weighted_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40 + 2 / 30)
    assert NOTE_SCALE in (state.note or "")

    add(store, 33.0, base + timedelta(hours=3), "new")  # the next increase counts again
    state = window_state(weighted_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40 + 5 / 30)
    assert state.estimated is True
    assert NOTE_SCALE in (state.note or "")

    # back to a pool without limit_base_usd: the NULL switch is also a new base
    add(store, 40.0, base + timedelta(hours=3, minutes=30), None)
    add(store, 41.0, NOW - timedelta(minutes=1), None)
    plain = weighted_pool(w, limit_base_usd=None, model_limits={})
    state = window_state(plain, w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40 + 6 / 30)
    assert NOTE_SCALE in (state.note or "")


def test_pool_without_base_unchanged(store) -> None:
    w = win(30.0)
    base = NOW - timedelta(hours=2)
    observe(store, w, 0.40, base)
    add(store, 10.0, base - timedelta(minutes=3), None)
    add(store, 11.0, base + timedelta(hours=1), None)
    add(store, 13.0, NOW - timedelta(minutes=1), None)
    plain = weighted_pool(w, limit_base_usd=None, model_limits={})
    state = window_state(plain, w, store, NOW, TOL)
    assert state.used == pytest.approx(0.50)
    assert state.estimated is True and state.note is None


def test_counter_drop_inside_chain_counts_only_growth(store) -> None:
    migrate_jobs_schema(store)
    w = win(30.0)
    base = NOW - timedelta(hours=2)
    observe(store, w, 0.40, base)
    add(store, 10.0, base, "h")
    add(store, 4.0, base + timedelta(hours=1), "h")  # counter dropped (stats reset)
    add(store, 7.0, NOW, "h")
    state = window_state(weighted_pool(w), w, store, NOW, TOL)
    assert state.used == pytest.approx(0.40 + 3 / 30)


# --- attempt cost estimation ----------------------------------------------------------

PRICES = '"glm-5.3" = [1.0, 4.0]\n'
TOKENS = (100_000, 10_000)  # raw price: 0.1 + 0.04 = 0.14 USD
RAW = 0.14
GO_MODEL = "opencode-go/glm-5.3#high"


def weighted_text(
    root: Path,
    backend: str,
    *,
    counter: bool,
    prices: Path,
    model: str = GO_MODEL,
    group: str | None = None,
    limits: str = '"glm-5.3" = 60\n',
) -> str:
    text = spend_config(root, backend, counter, model)
    keys = f'price_file = "{prices.as_posix()}"\nlimit_base_usd = 180\n'
    text = text.replace('unit = "usd"\n', f'unit = "usd"\n{keys}', 1)
    text = text.replace(
        "  [[pools.windows]]", f"[pools.model_limits]\n{limits}\n  [[pools.windows]]", 1
    )
    if group is not None:
        text = text.replace(f'model = "{model}"\n', f'model = "{model}"\nmodel_key = "{group}"\n')
    return text


@pytest.fixture
def weighted_rig(tmp_path, home, store, jobstore, clock, register_harness):
    rigs: list[SpendRig] = []

    def build(
        adapter,
        *,
        backend: str = "fake",
        spend: str = "journal",
        counter: bool = False,
        prices: str = PRICES,
        **fields,
    ) -> SpendRig:
        home.mkdir(parents=True, exist_ok=True)
        price_path = home / "prices weighted.toml"
        price_path.write_text(prices, encoding="utf-8")
        register_harness("fake", HarnessSpec("fake", ScriptAdapter(), ("fake",), "journal"))
        spec = HarnessSpec(backend, adapter, (backend,), spend)
        register_harness(backend, spec)
        rig = SpendRig(
            tmp_path,
            home,
            store,
            jobstore,
            clock,
            ImmediateExecutor(),
            ROUTES,
            specs={backend: spec},
            text_of=lambda root: weighted_text(
                root, backend, counter=counter, prices=price_path, **fields
            ),
        )
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


def spend_versions(rig: SpendRig) -> list[str | None]:
    rows = rig.jobs._store.connection().execute("SELECT prices_version FROM spend").fetchall()
    return [row[0] for row in rows]


def test_opencode_attempt_cost_estimated(weighted_rig) -> None:
    rig = weighted_rig(
        MeteredAdapter(cost=None, tokens=TOKENS), backend="opencode", spend="counter", counter=True
    )
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    (attempt,) = rig.attempts(job)
    assert attempt.spend_source == "counter"
    assert attempt.cost_usd == pytest.approx(RAW * 3)
    assert attempt.cost_estimated is True
    assert rig.rows() == []  # pool spend on the counter is tracked by the counter
    assert rig.bus.data("spend.recorded") == []


def test_journal_spend_scaled_once(weighted_rig) -> None:
    rig = weighted_rig(MeteredAdapter(cost=None, tokens=TOKENS))
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.cost_usd == pytest.approx(RAW * 3)
    assert attempt.cost_estimated is True
    (row,) = rig.rows()
    assert row["cost_usd"] == pytest.approx(RAW * 3)  # same value, weight applied once
    assert row["estimated"] == 1
    (version,) = spend_versions(rig)
    assert version is not None and len(version) == 12
    (event,) = rig.bus.data("spend.recorded")
    assert event["cost_usd"] == pytest.approx(RAW * 3) and event["estimated"] is True


def test_harness_cost_not_scaled(weighted_rig) -> None:
    rig = weighted_rig(MeteredAdapter(cost=0.12, tokens=TOKENS))
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.cost_usd == pytest.approx(0.12)
    assert attempt.cost_estimated is False
    (row,) = rig.rows()
    assert row["cost_usd"] == pytest.approx(0.12) and row["estimated"] == 0
    assert spend_versions(rig) == [None]


def test_no_price_keeps_cost_none(weighted_rig) -> None:
    rig = weighted_rig(MeteredAdapter(cost=None, tokens=TOKENS), prices='"other" = [1.0, 1.0]\n')
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.cost_usd is None and attempt.cost_estimated is False
    (row,) = rig.rows()
    assert row["cost_usd"] is None and row["estimated"] == 0


def test_no_tokens_keeps_cost_none(weighted_rig) -> None:
    rig = weighted_rig(MeteredAdapter(cost=None, tokens=(None, None)))
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.cost_usd is None and attempt.cost_estimated is False


def test_cost_survives_final_write_retry(weighted_rig, monkeypatch) -> None:
    rig = weighted_rig(MeteredAdapter(cost=None, tokens=TOKENS))
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
    assert attempt.cost_usd == pytest.approx(RAW * 3)
    assert attempt.cost_estimated is True
    (row,) = rig.rows()
    assert row["cost_usd"] == pytest.approx(RAW * 3) and row["estimated"] == 1


@pytest.mark.parametrize(
    ("prices", "limits", "expected"),
    [
        ('"glm-5.3" = [1.0, 4.0]\n', '"glm-5.3" = 60\n', RAW * 3),  # key is the route's model
        ('"glm-group" = [1.0, 4.0]\n', '"glm-5.3" = 60\n', RAW * 3),  # fallback: model_group
        ('"glm-5.3" = [1.0, 4.0]\n', '"glm-group" = 60\n', RAW),  # weight is looked up by model
    ],
    ids=["route-model", "group-fallback", "weight-by-model"],
)
def test_price_key_uses_route_model(weighted_rig, prices: str, limits: str, expected: float):
    rig = weighted_rig(
        MeteredAdapter(cost=None, tokens=TOKENS), prices=prices, limits=limits, group="glm-group"
    )
    job = rig.submit(rig.plain())
    rig.tick(2)
    (attempt,) = rig.attempts(job)
    assert attempt.cost_usd == pytest.approx(expected)
    assert attempt.cost_estimated is True


def test_estimate_attempt_cost_unit(home: Path, register_harness) -> None:
    register_harness("fake", HarnessSpec("fake", ScriptAdapter(), ("fake",), "journal"))
    config = write_config(
        home,
        weighted_text(home, "fake", counter=False, prices=_price_file(home)),
    )
    pool = next(item for item in config.pools if item.id == "pool-s")
    route = next(item for item in config.routes if item.id == "s/main")
    attempt = Attempt(
        "j_e",
        1,
        "s/main",
        "pool-s",
        NOW,
        finished_at=NOW,
        tokens_in=TOKENS[0],
        tokens_out=TOKENS[1],
    )
    estimate = estimate_attempt_cost(attempt, route, pool)
    assert estimate is not None
    cost, version = estimate
    assert cost == pytest.approx(RAW * 3)
    assert version == pool.prices.version

    paid = Attempt("j_e", 1, "s/main", "pool-s", NOW, finished_at=NOW, tokens_in=1, cost_usd=0.5)
    assert estimate_attempt_cost(paid, route, pool) is None  # harness cost is left alone
    again = Attempt(
        "j_e",
        1,
        "s/main",
        "pool-s",
        NOW,
        finished_at=NOW,
        tokens_in=TOKENS[0],
        tokens_out=TOKENS[1],
        cost_usd=RAW * 3,
        cost_estimated=True,
    )
    repeated = estimate_attempt_cost(again, route, pool)  # re-estimating yields the same value
    assert repeated is not None and repeated[0] == pytest.approx(RAW * 3)

    estimated = Attempt(
        "j_e", 1, "s/main", "pool-s", NOW, finished_at=NOW, cost_usd=0.42, cost_estimated=True
    )
    entry = entry_for_attempt(estimated, version)
    assert (entry.cost_usd, entry.estimated, entry.prices_version) == (0.42, True, version)
    harness = entry_for_attempt(paid, None)
    assert (harness.cost_usd, harness.estimated, harness.prices_version) == (0.5, False, None)


def _price_file(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path
