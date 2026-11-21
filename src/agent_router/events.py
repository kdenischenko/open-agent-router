"""In-process event bus: "state changed" notifications.

Events carry no state: the subscriber re-reads the store, so a lost event breaks
nothing. The publisher never waits for subscribers: each has its own bounded queue
and its own thread.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_router.clock import Clock

FORBIDDEN_PARTS = frozenset(
    {
        "prompt",
        "text",
        "output",
        "token",
        "cwd",
        "path",
        "password",
        "secret",
        "key",
        "authorization",
    }
)
FORBIDDEN_SUBSTRINGS = frozenset(
    {
        "apikey",
        "passwd",
        "passphrase",
        "credential",
        "cookie",
        "bearer",
        "secret",
        "password",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "idtoken",
        "sessiontoken",
        "sessionkey",
    }
)


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    at: datetime
    data: Mapping[str, Any]


def _key_parts(key: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", key)
    return {part.lower() for part in re.split(r"[^a-zA-Z0-9]+", separated) if part}


def _check_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("_", "").replace("-", "").replace(" ", "")
            if _key_parts(key) & FORBIDDEN_PARTS or any(
                part in normalized for part in FORBIDDEN_SUBSTRINGS
            ):
                raise ValueError(f'event data key "{key}" is forbidden')
            _check_keys(item)
    elif isinstance(value, list):
        for item in value:
            _check_keys(item)


def _clean(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Data copy via a strict JSON round-trip; secrets and job content do not
    belong here.
    """
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise TypeError("event data must be a dict")
    copy = json.loads(json.dumps(dict(data), allow_nan=False))
    _check_keys(copy)
    return copy


class _Subscriber:
    def __init__(
        self,
        name: str,
        handler: Callable[[Event], None],
        lock: threading.Lock,
        on_abandoned: Callable[[], None] | None,
    ) -> None:
        self.name = name
        self.handler = handler
        self.on_abandoned = on_abandoned
        self.queue: deque[Event] = deque()
        self.wake = threading.Condition(lock)
        self.stopping = False  # drain the queue and exit
        self.abandoned = False  # close deadline expired: exit after the current handler
        self.delivered = 0
        self.dropped = 0
        self.errors = 0
        self.thread: threading.Thread | None = None


class EventBus:
    def __init__(self, clock: Clock, queue_size: int = 1000) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self._clock = clock
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._seq = 0
        self._closed = False
        self._subscribers: dict[str, _Subscriber] = {}

    @property
    def seq(self) -> int:
        """Sequence number of the last event; 0 means no events yet."""
        with self._lock:
            return self._seq

    def publish(self, type: str, data: Mapping[str, Any] | None = None) -> Event:
        if not isinstance(type, str) or not type:
            raise ValueError("event type must be a non-empty string")
        payload = _clean(data)
        with self._lock:  # single critical section: sequence number, queues, notification
            self._seq += 1
            event = Event(seq=self._seq, type=type, at=self._clock.now(), data=payload)
            if not self._closed:
                for sub in self._subscribers.values():
                    if len(sub.queue) >= self._queue_size:
                        sub.dropped += 1
                    else:
                        sub.queue.append(event)
                        sub.wake.notify()
            self._changed.notify_all()
        return Event(
            seq=event.seq,
            type=event.type,
            at=event.at,
            data=copy.deepcopy(event.data),
        )

    def wait(self, after_seq: int, timeout: float) -> int:
        """Wait until the sequence number exceeds after_seq, at most timeout;
        return the current number.
        """
        with self._lock:
            self._changed.wait_for(lambda: self._seq > after_seq, timeout=max(0.0, timeout))
            return self._seq

    def subscribe(
        self,
        name: str,
        handler: Callable[[Event], None],
        on_abandoned: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("event bus is closed")
            if name in self._subscribers:
                raise ValueError(f'subscriber "{name}" already exists')
            sub = _Subscriber(name, handler, self._lock, on_abandoned)
            sub.thread = threading.Thread(
                target=self._run, args=(sub,), name=f"event-sub-{name}", daemon=True
            )
            self._subscribers[name] = sub
            try:
                sub.thread.start()  # under the lock: close() will not see an unstarted thread
            except BaseException:
                del self._subscribers[name]
                raise

    def _run(self, sub: _Subscriber) -> None:
        while True:
            with self._lock:
                while not sub.queue and not sub.stopping:
                    sub.wake.wait()
                if sub.abandoned or not sub.queue:
                    on_abandoned = sub.on_abandoned if sub.abandoned else None
                    break
                event = sub.queue.popleft()
            try:
                delivered = Event(
                    seq=event.seq,
                    type=event.type,
                    at=event.at,
                    data=copy.deepcopy(event.data),
                )
                sub.handler(delivered)  # outside the critical section: the publisher never waits
            except Exception:
                with self._lock:
                    sub.errors += 1
            else:
                with self._lock:
                    sub.delivered += 1
        if on_abandoned is not None:
            with contextlib.suppress(Exception):
                on_abandoned()

    def stats(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                name: {"delivered": sub.delivered, "dropped": sub.dropped, "errors": sub.errors}
                for name, sub in self._subscribers.items()
            }

    def close(self, timeout: float = 2) -> list[str]:
        """Deliver the remaining events within the overall deadline; return names of subscribers
        that failed to stop in time.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers.values())
            for sub in subscribers:
                sub.stopping = True
                sub.wake.notify()
        current = threading.current_thread()
        for sub in subscribers:
            # close() from a handler: do not join our own thread, it will land on the stuck list
            if sub.thread is not None and sub.thread is not current:
                sub.thread.join(max(0.0, deadline - time.monotonic()))
        stuck: list[str] = []
        with self._lock:
            for sub in subscribers:
                if sub.thread is not None and sub.thread.is_alive():
                    sub.abandoned = True
                    sub.dropped += len(sub.queue)
                    sub.queue.clear()
                    stuck.append(sub.name)
        return stuck
