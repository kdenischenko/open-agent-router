"""Classifier vote queue and the flags coordinating live voting with offline eval."""

from __future__ import annotations

import contextlib
import heapq
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from agent_router.classifier_backends import classify_one, post
from agent_router.clock import to_utc
from agent_router.config import ClassifierModel, ClassifierSettings
from agent_router.jobs import Vote
from agent_router.runner import process_image

CLASSIFY_BACKLOG = 64
EVAL_FLAG = "classifier_eval_running"
BUSY_FLAG = "classifier_live_busy"
_SECONDARY_DROP_S = 600.0  # drop a non-primary vote older than 10 minutes
_FLAG_STALE_S = 2 * 3600.0  # a flag older than two hours is considered stale


def flag_value(pid: int, moment: datetime) -> str:
    """Flag value string: pid and timestamp, used to check that another process is still alive."""
    return f"{int(pid)}|{to_utc(moment).isoformat()}"


def _default_alive(pid: int) -> bool:
    try:
        return process_image(pid) is not None
    except Exception:
        return True


def _flag_fresh(raw: str | None, now: datetime, alive: Callable[[int], bool]) -> bool:
    if not raw:
        return False
    pid_text, separator, moment_text = raw.partition("|")
    if not separator:
        return False
    try:
        pid = int(pid_text)
        moment = to_utc(datetime.fromisoformat(moment_text))
    except (TypeError, ValueError):
        return False
    if now - moment > timedelta(seconds=_FLAG_STALE_S):
        return False
    try:
        return alive(pid)
    except Exception:
        return True


class VoteQueue:
    """Single priority queue of votes: primary first, one worker thread."""

    def __init__(
        self,
        settings: ClassifierSettings,
        *,
        now: Callable[[], datetime],
        flags: Any = None,
        post: Callable[..., Any] = post,
        clock: Callable[[], float] = time.monotonic,
        alive: Callable[[int], bool] = _default_alive,
        backlog: int = CLASSIFY_BACKLOG,
        start: bool = True,
    ) -> None:
        self._now = now
        self._flags = flags
        self._post = post
        self._clock = clock
        self._alive = alive
        self._backlog = backlog
        self._cond = threading.Condition(threading.Lock())
        self._heap: list[tuple[int, int, str, str, datetime, ClassifierModel]] = []
        self._seq = 0
        self._ready: list[tuple[str, Vote]] = []
        self._closed = False
        self._thread: threading.Thread | None = None
        self._models = tuple(item for item in settings.effective_models if item.live)
        primary = settings.primary_model
        self.primary = None if primary is None else primary.id
        if start and self._models:
            self._thread = threading.Thread(target=self._run, name="classifier", daemon=True)
            self._thread.start()

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def submit(self, job_id: str, prompt: str, created_at: datetime) -> None:
        with self._cond:
            if self._closed:
                return
            for model in self._models:
                is_primary = model.id == self.primary
                # the backlog limits only non-primary votes; primary is bounded by the job count
                if not is_primary and len(self._heap) >= self._backlog:
                    self._ready.append(
                        (job_id, Vote(model.id, model.model, None, None, 0, "dropped"))
                    )
                    continue
                priority = 0 if is_primary else 1
                heapq.heappush(self._heap, (priority, self._seq, job_id, prompt, created_at, model))
                self._seq += 1
            self._cond.notify()

    def _eval_fresh(self) -> bool:
        if self._flags is None:
            return False
        try:
            return _flag_fresh(self._flags.get(EVAL_FLAG), self._now(), self._alive)
        except Exception:
            return False

    def step(self) -> bool:
        """One unit of work, synchronously; False — queue is empty, closed, or eval is running."""
        with self._cond:
            if self._closed or self._eval_fresh() or not self._heap:
                return False
            priority, seq, job_id, prompt, created_at, model = heapq.heappop(self._heap)
            if priority == 1 and self._now() - created_at > timedelta(seconds=_SECONDARY_DROP_S):
                self._ready.append((job_id, Vote(model.id, model.model, None, None, 0, "dropped")))
                return True
        mine = flag_value(os.getpid(), self._now())
        hold: str | None = None
        if self._flags is not None:
            try:
                if not self._flags.claim_unless(
                    BUSY_FLAG,
                    mine,
                    EVAL_FLAG,
                    blocked=lambda cur: _flag_fresh(cur, self._now(), self._alive),
                ):
                    # eval claimed the flag first: put the item back with the same priority
                    with self._cond:
                        heapq.heappush(
                            self._heap, (priority, seq, job_id, prompt, created_at, model)
                        )
                    return False
                hold = mine
            except Exception:
                hold = None  # flags store unavailable — run the request without the flag
        try:
            vote = classify_one(prompt, model, post=self._post, clock=self._clock)
        finally:
            if hold is not None:
                with contextlib.suppress(Exception):
                    self._flags.release(BUSY_FLAG, hold)
        with self._cond:
            self._ready.append((job_id, vote))
        return True

    def drain(self) -> list[tuple[str, Vote]]:
        with self._cond:
            ready, self._ready = self._ready, []
            return ready

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
            if self.step():
                continue
            with self._cond:
                if self._closed:
                    return
                self._cond.wait(timeout=1.0)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join()
