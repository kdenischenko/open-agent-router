"""Store for observations and counter readings: the Store protocol and a SQLite implementation."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from agent_router import plugins
from agent_router.clock import to_utc
from agent_router.config import ConfigError, StorageSettings

STORAGE_GROUP = "agent_router.storage"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_us(moment: datetime) -> int:
    """Moment -> whole epoch microseconds (exact storage and comparison)."""
    return (to_utc(moment) - _EPOCH) // timedelta(microseconds=1)


def from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


@dataclass(frozen=True)
class Observation:
    pool: str
    window: str
    fraction: float
    observed_at: datetime
    resets_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        if isinstance(self.fraction, bool) or not isinstance(self.fraction, int | float):
            raise ValueError("fraction must be a number")
        if not 0.0 <= self.fraction <= 1.0:  # NaN also ends up here
            raise ValueError(f"fraction outside 0..1: {self.fraction!r}")
        object.__setattr__(self, "fraction", float(self.fraction))
        object.__setattr__(self, "observed_at", to_utc(self.observed_at))
        if self.resets_at is not None:
            object.__setattr__(self, "resets_at", to_utc(self.resets_at))


@dataclass(frozen=True)
class CounterSample:
    pool: str
    value_usd: float
    sampled_at: datetime
    scale_hash: str | None = None  # model-weight fingerprint; None: old sample or no weights

    def __post_init__(self) -> None:
        if isinstance(self.value_usd, bool) or not isinstance(self.value_usd, int | float):
            raise ValueError("counter value must be a number")
        if not self.value_usd >= 0.0 or self.value_usd == float("inf"):
            raise ValueError(f"counter value must be finite and non-negative: {self.value_usd!r}")
        object.__setattr__(self, "value_usd", float(self.value_usd))
        object.__setattr__(self, "sampled_at", to_utc(self.sampled_at))


class Store(Protocol):
    """The core only knows this protocol."""

    def close(self) -> None: ...

    def add_observation(self, obs: Observation) -> bool:
        """True if a new row was inserted, False if the last row's time was updated."""
        ...

    def last_observation(self, pool: str, window: str) -> Observation | None: ...

    def observations(self, pool: str, window: str, limit: int = 100) -> list[Observation]:
        """Window history, newest first."""
        ...

    def add_counter_sample(self, sample: CounterSample) -> None: ...

    def counter_at(self, pool: str, moment: datetime, tolerance: timedelta) -> CounterSample | None:
        """Sample nearest to `moment` from either side, when within `tolerance`."""
        ...

    def last_counter(self, pool: str) -> CounterSample | None: ...

    def counter_samples_since(self, pool: str, since: datetime) -> list[CounterSample]:
        """Pool samples with `sampled_at >= since`, time-ordered; the anchor sample is included."""
        ...


_SCHEMA_V1 = (
    """
    CREATE TABLE observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool TEXT NOT NULL,
        window TEXT NOT NULL,
        fraction REAL NOT NULL,
        observed_at INTEGER NOT NULL,
        resets_at INTEGER,
        source TEXT NOT NULL
    )
    """,
    "CREATE INDEX observations_window ON observations (pool, window, observed_at, id)",
    """
    CREATE TABLE counter_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool TEXT NOT NULL,
        value_usd REAL NOT NULL,
        sampled_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX counter_samples_pool ON counter_samples (pool, sampled_at, id)",
)


class SqliteStore:
    """SQLite: WAL, synchronous=NORMAL, 5 s busy_timeout, one connection shared by all threads.

    The gateway serves HTTP on worker threads that are retired and replaced as traffic
    comes and goes; per-thread connections would accumulate without ever being closed.
    One connection (check_same_thread=False) serves every thread instead, and the store
    lock is held for the whole of every statement sequence, so operations on it never
    interleave.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Reentrant: a transaction holds the lock while its body calls back into the store.
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._scale_hash_column: bool | None = None  # caches a positive answer
        self.migrate(1, _SCHEMA_V1)

    # --- connection and schema ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """The shared connection; created on first use. Callers hold the store lock."""
        if self._conn is None:
            if self._closed:
                raise RuntimeError("store is closed")
            self._conn = sqlite3.connect(
                self.path, timeout=5, isolation_level=None, check_same_thread=False
            )
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        return self._conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """The shared connection; the store lock is held while the block is open."""
        with self._lock:
            yield self._connect()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction: takes the store lock, then the SQLite write lock; reads and
        writes inside are atomic."""
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def schema_version(self) -> int:
        with self.connection() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self, version: int, statements: Sequence[str]) -> None:
        """Migrate schema to `version` (from `version - 1`); a completed migration is skipped."""
        with self.transaction() as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current >= version:
                return
            if current != version - 1:
                raise RuntimeError(
                    f"schema at version {current}: migration to {version} out of order"
                )
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(version)}")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- observations ---------------------------------------------------------

    _OBS_COLUMNS = "pool, window, fraction, observed_at, resets_at, source"

    @staticmethod
    def _observation(row: Sequence[Any]) -> Observation:
        return Observation(
            pool=row[0],
            window=row[1],
            fraction=row[2],
            observed_at=from_us(row[3]),
            resets_at=None if row[4] is None else from_us(row[4]),
            source=row[5],
        )

    def add_observation(self, obs: Observation) -> bool:
        observed = to_us(obs.observed_at)
        resets = None if obs.resets_at is None else to_us(obs.resets_at)
        with self.transaction() as conn:
            last = conn.execute(
                "SELECT id, fraction, observed_at, resets_at, source FROM observations"
                " WHERE pool = ? AND window = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
                (obs.pool, obs.window),
            ).fetchone()
            same = (
                last is not None
                and last[4] == obs.source
                and last[1] == obs.fraction
                and last[3] == resets
            )
            if same:
                conn.execute(
                    "UPDATE observations SET observed_at = ? WHERE id = ?",
                    (max(observed, last[2]), last[0]),
                )
                return False
            conn.execute(
                f"INSERT INTO observations ({self._OBS_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (obs.pool, obs.window, obs.fraction, observed, resets, obs.source),
            )
            return True

    def last_observation(self, pool: str, window: str) -> Observation | None:
        found = self.observations(pool, window, limit=1)
        return found[0] if found else None

    def observations(self, pool: str, window: str, limit: int = 100) -> list[Observation]:
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._OBS_COLUMNS} FROM observations WHERE pool = ? AND window = ?"
                " ORDER BY observed_at DESC, id DESC LIMIT ?",
                (pool, window, int(limit)),
            ).fetchall()
        return [self._observation(row) for row in rows]

    # --- counter ------------------------------------------------------------

    def _has_scale_hash(self) -> bool:
        """Whether counter_samples.scale_hash exists; older databases may not have it yet."""
        if self._scale_hash_column:
            return True
        with self.connection() as conn:
            rows = conn.execute("PRAGMA table_info(counter_samples)").fetchall()
        present = any(row[1] == "scale_hash" for row in rows)
        if present:
            self._scale_hash_column = True
        return present

    def _counter_columns(self) -> str:
        if self._has_scale_hash():
            return "pool, value_usd, sampled_at, scale_hash"
        return "pool, value_usd, sampled_at"

    @staticmethod
    def _sample(row: Sequence[Any] | None) -> CounterSample | None:
        if row is None:
            return None
        scale_hash = row[3] if len(row) > 3 else None
        return CounterSample(
            pool=row[0], value_usd=row[1], sampled_at=from_us(row[2]), scale_hash=scale_hash
        )

    def add_counter_sample(self, sample: CounterSample) -> None:
        with self.transaction() as conn:
            if self._has_scale_hash():
                conn.execute(
                    "INSERT INTO counter_samples (pool, value_usd, sampled_at, scale_hash)"
                    " VALUES (?, ?, ?, ?)",
                    (sample.pool, sample.value_usd, to_us(sample.sampled_at), sample.scale_hash),
                )
            elif sample.scale_hash is not None:
                raise RuntimeError(
                    "database has no counter_samples.scale_hash column:"
                    " upgrade the store schema to version 9"
                )
            else:
                conn.execute(
                    "INSERT INTO counter_samples (pool, value_usd, sampled_at) VALUES (?, ?, ?)",
                    (sample.pool, sample.value_usd, to_us(sample.sampled_at)),
                )

    def counter_at(self, pool: str, moment: datetime, tolerance: timedelta) -> CounterSample | None:
        moment = to_utc(moment)
        at = to_us(moment)
        columns = self._counter_columns()
        with self.connection() as conn:
            past = conn.execute(
                f"SELECT {columns} FROM counter_samples"
                " WHERE pool = ? AND sampled_at <= ? ORDER BY sampled_at DESC, id DESC LIMIT 1",
                (pool, at),
            ).fetchone()
            future = conn.execute(
                f"SELECT {columns} FROM counter_samples"
                " WHERE pool = ? AND sampled_at > ? ORDER BY sampled_at ASC, id ASC LIMIT 1",
                (pool, at),
            ).fetchone()
        candidates = [s for s in (self._sample(past), self._sample(future)) if s is not None]
        sample = min(
            candidates, key=lambda s: (abs(s.sampled_at - moment), s.sampled_at), default=None
        )
        if sample is None or abs(sample.sampled_at - moment) > tolerance:
            return None
        return sample

    def last_counter(self, pool: str) -> CounterSample | None:
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT {self._counter_columns()} FROM counter_samples"
                " WHERE pool = ? ORDER BY sampled_at DESC, id DESC LIMIT 1",
                (pool,),
            ).fetchone()
        return self._sample(row)

    def counter_samples_since(self, pool: str, since: datetime) -> list[CounterSample]:
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._counter_columns()} FROM counter_samples"
                " WHERE pool = ? AND sampled_at >= ? ORDER BY sampled_at, id",
                (pool, to_us(since)),
            ).fetchall()
        return [self._sample(row) for row in rows]


def plugin_call(group: str, name: str, function: str, *args: Any) -> Any:
    """Call a backend plugin function; any failure becomes a ConfigError naming the plugin."""
    plugin = plugins.load_plugin(group, name)
    target = getattr(plugin, function, None)
    if not callable(target):
        raise ConfigError([f'plugin "{name}" in group {group}: no function {function}'])
    try:
        return target(*args)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError([f'plugin "{name}" in group {group}: {function} failed: {exc}']) from exc


def open_store(settings: StorageSettings, home: Path) -> Store:
    """Open the store from settings: built-in SQLite or a named third-party backend."""
    if settings.backend == "sqlite":
        return SqliteStore(Path(home) / "agent-router.sqlite")
    options: Mapping[str, Any] = settings.options
    return plugin_call(STORAGE_GROUP, settings.backend, "open_store", options, Path(home))
