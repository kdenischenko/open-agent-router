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
        conn = store.connection()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        seen = []
        thread = threading.Thread(target=lambda: seen.append(store.connection()))
        thread.start()
        thread.join()
        assert seen[0] is not conn  # one connection per thread
    finally:
        store.close()
    store.close()  # closing twice does not raise


def test_sqlite_connections_from_finished_threads_are_closed(tmp_path):
    store = SqliteStore(tmp_path / "a.sqlite")
    finished_connections: list[sqlite3.Connection] = []
    errors: list[BaseException] = []

    def open_and_finish() -> None:
        try:
            conn = store.connection()
            conn.execute("select 1").fetchone()
            finished_connections.append(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_and_finish) for _ in range(50)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(finished_connections) == 50

        ready = threading.Event()
        release = threading.Event()

        def open_and_wait() -> None:
            try:
                store.connection().execute("select 1").fetchone()
                ready.set()
                release.wait(timeout=10)
            except BaseException as exc:
                errors.append(exc)

        keeper = threading.Thread(target=open_and_wait)
        keeper.start()
        assert ready.wait(timeout=10)
        try:
            assert len(store._connections) <= 2
            for conn in finished_connections:
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute("select 1")
        finally:
            release.set()
            keeper.join(timeout=10)
        assert not keeper.is_alive()
        assert errors == []
    finally:
        store.close()


def test_sqlite_cleanup_keeps_live_thread_connection(tmp_path):
    store = SqliteStore(tmp_path / "a.sqlite")
    ready = threading.Event()
    release = threading.Event()
    live_connections: list[sqlite3.Connection] = []
    errors: list[BaseException] = []

    def open_and_wait() -> None:
        try:
            live_connections.append(store.connection())
            ready.set()
            release.wait(timeout=10)
        except BaseException as exc:
            errors.append(exc)

    def open_and_finish() -> None:
        try:
            store.connection().execute("select 1").fetchone()
        except BaseException as exc:
            errors.append(exc)

    live_thread = threading.Thread(target=open_and_wait)
    live_thread.start()
    try:
        assert ready.wait(timeout=10)
        other_thread = threading.Thread(target=open_and_finish)
        other_thread.start()
        other_thread.join(timeout=10)
        assert not other_thread.is_alive()
        assert live_connections[0].execute("select 1").fetchone() == (1,)
        assert errors == []
    finally:
        release.set()
        live_thread.join(timeout=10)
        store.close()
    assert not live_thread.is_alive()


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
