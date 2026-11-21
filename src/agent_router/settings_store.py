"""Gateway settings in the settings table: key and string value."""

from __future__ import annotations

from collections.abc import Callable

from agent_router.store import Store


class SqliteSettings:
    """String settings in SQLite: key -> value."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, key: str) -> str | None:
        row = (
            self._store.connection()
            .execute("SELECT value FROM settings WHERE key = ?", (key,))
            .fetchone()
        )
        return None if row is None else row[0]

    def set(self, key: str, value: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def claim(self, key: str, value: str, free: Callable[[str | None], bool]) -> str | None:
        """Atomically claims `key` when its current value may be replaced freely.

        Returns the current value if the key is taken (`free` is false); otherwise writes
        `value` and returns None. Read and write happen in one `BEGIN IMMEDIATE` transaction.
        """
        with self._store.transaction() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            current = None if row is None else row[0]
            if current is not None and not free(current):
                return current
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return None

    def claim_unless(
        self, key: str, value: str, blocker: str, blocked: Callable[[str | None], bool]
    ) -> bool:
        """Atomically writes `key = value` only if `blocker` is not locked.

        `blocked` reads the current value of `blocker`; the check and the write happen in
        one transaction. True means the value was written, False means the lock is taken
        and nothing was written.
        """
        with self._store.transaction() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (blocker,)).fetchone()
            current = None if row is None else row[0]
            if blocked(current):
                return False
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return True

    def release(self, key: str, value: str) -> bool:
        """Releases `key` only if its value is the owner's `value`."""
        with self._store.transaction() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ? AND value = ?", (key, value))
            return cursor.rowcount > 0

    def bump(self, key: str) -> int:
        """Atomically increments the value by one; returns 1 when there is no row."""
        with self._store.transaction() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            value = 1 if row is None else int(row[0]) + 1
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            return value
