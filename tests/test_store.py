from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from agent_router import plugins
from agent_router.config import ConfigError, StorageSettings
from agent_router.store import CounterSample, Observation, SqliteStore, open_store

T0 = datetime(2026, 11, 12, 10, tzinfo=UTC)


def obs(fraction=0.4, at=T0, resets=None, source="manual", pool="p", window="5h"):
    return Observation(pool, window, fraction, at, resets, source)


# --- store protocol (store fixture parametrized over backends) ----------------


def test_write_and_read(store):
    resets = T0 + timedelta(hours=3)
    assert store.last_observation("p", "5h") is None
    store.add_observation(obs(0.42, resets=resets))
    got = store.last_observation("p", "5h")
    assert got == Observation("p", "5h", 0.42, T0, resets, "manual")
    assert got.observed_at.tzinfo is not None
    assert store.last_observation("p", "week") is None
    assert store.last_observation("other", "5h") is None


def test_repeat_updates_time_instead_of_adding_row(store):
    assert store.add_observation(obs(0.4)) is True
    later = T0 + timedelta(minutes=5)
    assert store.add_observation(obs(0.4, at=later)) is False
    rows = store.observations("p", "5h")
    assert len(rows) == 1
    assert rows[0].observed_at == later


def test_other_value_source_or_reset_adds_row(store):
    store.add_observation(obs(0.4))
    assert store.add_observation(obs(0.5, at=T0 + timedelta(minutes=1))) is True
    assert store.add_observation(obs(0.5, at=T0 + timedelta(minutes=2), source="x")) is True
    reset = T0 + timedelta(hours=1)
    assert store.add_observation(obs(0.5, at=T0 + timedelta(minutes=3), source="x", resets=reset))
    assert len(store.observations("p", "5h")) == 4


def test_repeat_with_older_time_does_not_move_time_back(store):
    store.add_observation(obs(0.4, at=T0))
    assert store.add_observation(obs(0.4, at=T0 - timedelta(hours=1))) is False
    assert store.last_observation("p", "5h").observed_at == T0


@pytest.mark.parametrize("fraction", [-0.01, 1.01, float("nan"), float("inf"), "0.5", None, True])
def test_fraction_out_of_range_rejected(fraction):
    with pytest.raises(ValueError):
        obs(fraction)


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        obs(at=datetime(2026, 11, 12, 10))
    with pytest.raises(ValueError):
        obs(resets=datetime(2026, 11, 12, 10))
    with pytest.raises(ValueError):
        CounterSample("p", 1.0, datetime(2026, 11, 12, 10))
    with pytest.raises(ValueError):
        CounterSample("p", -1.0, T0)


def test_last_observation_by_observed_at_not_insert_order(store):
    store.add_observation(obs(0.7, at=T0 + timedelta(hours=1)))
    store.add_observation(obs(0.2, at=T0))
    assert store.last_observation("p", "5h").fraction == 0.7


def test_last_observation_tie_takes_later_insert(store):
    store.add_observation(obs(0.2, at=T0))
    store.add_observation(obs(0.3, at=T0))
    assert store.last_observation("p", "5h").fraction == 0.3


def test_counter_at_sample_before_moment(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=20)))
    store.add_counter_sample(CounterSample("p", 11.0, T0 - timedelta(minutes=4)))
    store.add_counter_sample(CounterSample("p", 12.0, T0 + timedelta(minutes=5)))
    got = store.counter_at("p", T0, timedelta(minutes=15))
    assert got == CounterSample("p", 11.0, T0 - timedelta(minutes=4))


def test_counter_at_only_after_moment(store):
    store.add_counter_sample(CounterSample("p", 13.0, T0 + timedelta(minutes=9)))
    store.add_counter_sample(CounterSample("p", 12.0, T0 + timedelta(minutes=3)))
    assert store.counter_at("p", T0, timedelta(minutes=15)).value_usd == 12.0


def test_counter_at_both_outside_tolerance(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=16)))
    store.add_counter_sample(CounterSample("p", 12.0, T0 + timedelta(minutes=16)))
    assert store.counter_at("p", T0, timedelta(minutes=15)) is None


def test_counter_at_only_after_and_too_far(store):
    store.add_counter_sample(CounterSample("p", 12.0, T0 + timedelta(minutes=16)))
    assert store.counter_at("p", T0, timedelta(minutes=15)) is None


def test_counter_at_no_samples(store):
    assert store.counter_at("p", T0, timedelta(minutes=15)) is None
    assert store.last_counter("p") is None


def test_counter_at_exact_moment_and_tolerance_edge(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=15)))
    assert store.counter_at("p", T0, timedelta(minutes=15)).value_usd == 10.0
    store.add_counter_sample(CounterSample("p", 11.0, T0))
    assert store.counter_at("p", T0, timedelta(0)).value_usd == 11.0


def test_last_counter_and_pool_isolation(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0))
    store.add_counter_sample(CounterSample("p", 12.5, T0 + timedelta(minutes=5)))
    store.add_counter_sample(CounterSample("q", 99.0, T0 + timedelta(minutes=9)))
    assert store.last_counter("p") == CounterSample("p", 12.5, T0 + timedelta(minutes=5))
    assert store.counter_at("p", T0 + timedelta(minutes=9), timedelta(minutes=15)).value_usd == 12.5


def test_four_threads_write_without_errors(store):
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def writer(n: int) -> None:
        try:
            start.wait(timeout=10)
            for i in range(50):
                value = (n * 50 + i) / 1000
                store.add_observation(obs(value, at=T0 + timedelta(seconds=n * 50 + i)))
                store.add_counter_sample(CounterSample("p", float(i), T0 + timedelta(seconds=i)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    rows = store.observations("p", "5h", limit=1000)
    assert len(rows) == 200
    assert len({row.fraction for row in rows}) == 200


# --- SQLite ---------------------------------------------------------------------


def test_sqlite_reopen_keeps_data(tmp_path):
    directory = "каталог с пробелом"  # non-ascii on purpose
    path = tmp_path / directory / "agent-router.sqlite"
    first = SqliteStore(path)
    first.add_observation(obs(0.33))
    first.add_counter_sample(CounterSample("p", 7.0, T0))
    assert first.schema_version() == 1
    first.close()
    second = SqliteStore(path)
    try:
        assert second.last_observation("p", "5h").fraction == 0.33
        assert second.last_counter("p").value_usd == 7.0
        assert second.schema_version() == 1
    finally:
        second.close()


def test_sqlite_settings(tmp_path):
    store = SqliteStore(tmp_path / "a.sqlite")
    try:
        with store.connection() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        seen = []

        def probe():
            with store.connection() as worker_conn:
                seen.append(worker_conn)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        assert seen[0] is conn  # one shared connection for every thread
    finally:
        store.close()
    store.close()  # closing twice does not raise
    with pytest.raises(RuntimeError), store.connection():
        pass


def test_sqlite_connections_do_not_accumulate_for_retired_threads(tmp_path, monkeypatch):
    """The gateway serves requests on worker threads that are retired after a while;
    retired workers must not leave open connections behind."""
    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    store = SqliteStore(tmp_path / "churn.sqlite")
    try:
        for _ in range(25):
            seen: list[CounterSample | None] = []

            def worker(out: list[CounterSample | None]) -> None:
                store.add_counter_sample(CounterSample("p", 1.0, T0))
                out.append(store.last_counter("p"))

            thread = threading.Thread(target=worker, args=(seen,))
            thread.start()
            thread.join(timeout=10)
            assert seen == [CounterSample("p", 1.0, T0)]  # the retired worker could read
        assert len(created) == 1  # one connection for all workers, not one per worker
        assert store.last_counter("p") == CounterSample("p", 1.0, T0)
    finally:
        store.close()


def test_sqlite_migrate_is_ordered_and_idempotent(tmp_path):
    store = SqliteStore(tmp_path / "a.sqlite")
    try:
        with pytest.raises(RuntimeError):
            store.migrate(3, ["CREATE TABLE t3 (x)"])
        store.migrate(2, ["CREATE TABLE t2 (x)"])
        store.migrate(2, ["CREATE TABLE t2 (x)"])  # repeat is skipped
        assert store.schema_version() == 2
        with pytest.raises(sqlite3.OperationalError):  # a failing step rolls back the migration
            store.migrate(3, ["CREATE TABLE t3 (x)", "no such SQL"])
        assert store.schema_version() == 2
    finally:
        store.close()


# --- open_store and plugins -----------------------------------------------------


def test_open_store_sqlite_default(tmp_path):
    store = open_store(StorageSettings(), tmp_path)
    try:
        assert isinstance(store, SqliteStore)
        assert store.path == tmp_path / "agent-router.sqlite"
    finally:
        store.close()


def test_open_store_unknown_backend(tmp_path):
    with pytest.raises(ConfigError) as caught:
        open_store(StorageSettings(backend="no-such-backend"), tmp_path)
    assert "no-such-backend" in str(caught.value)


def test_open_store_uses_plugin(tmp_path, monkeypatch):
    calls = []

    class Plugin:
        @staticmethod
        def open_store(options, home):
            calls.append((dict(options), home))
            return "plugin-store"

    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: Plugin)
    settings = StorageSettings(backend="pg", options={"dsn": "x"})
    assert open_store(settings, tmp_path) == "plugin-store"
    assert calls == [({"dsn": "x"}, tmp_path)]


def test_open_store_plugin_without_function(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: object())
    with pytest.raises(ConfigError) as caught:
        open_store(StorageSettings(backend="pg"), tmp_path)
    assert "pg" in str(caught.value)


def test_open_store_plugin_raises(tmp_path, monkeypatch):
    class Plugin:
        @staticmethod
        def open_store(options, home):
            raise OSError("no connection")

    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: Plugin)
    with pytest.raises(ConfigError) as caught:
        open_store(StorageSettings(backend="pg"), tmp_path)
    assert "pg" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)


def test_counter_at_prefers_nearest_sample_across_moment(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=20)))
    store.add_counter_sample(CounterSample("p", 11.0, T0 + timedelta(minutes=10)))
    got = store.counter_at("p", T0, timedelta(minutes=15))
    assert got == CounterSample("p", 11.0, T0 + timedelta(minutes=10))


def test_counter_at_keeps_past_sample_when_nearer(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=5)))
    store.add_counter_sample(CounterSample("p", 11.0, T0 + timedelta(minutes=20)))
    got = store.counter_at("p", T0, timedelta(minutes=15))
    assert got == CounterSample("p", 10.0, T0 - timedelta(minutes=5))


def test_counter_at_equal_distance_takes_earlier(store):
    store.add_counter_sample(CounterSample("p", 11.0, T0 + timedelta(minutes=10)))
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=10)))
    assert store.counter_at("p", T0, timedelta(minutes=15)).value_usd == 10.0


def test_counter_at_none_outside_tolerance(store):
    store.add_counter_sample(CounterSample("p", 10.0, T0 - timedelta(minutes=30)))
    store.add_counter_sample(CounterSample("p", 11.0, T0 + timedelta(minutes=40)))
    assert store.counter_at("p", T0, timedelta(minutes=15)) is None
