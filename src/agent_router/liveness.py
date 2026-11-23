"""Liveness of a running harness: silence past the idle limit, and the hard time cap."""

from __future__ import annotations

from typing import Literal

Verdict = Literal["ok", "idle", "hard"]
Signal = str


class Liveness:
    """Tracks the last harness output and answers whether the attempt should go on.

    The caller owns the clock: every moment is passed in, so the class stays testable
    without a real clock and works with `time.monotonic()` in the runner.

    A touch records which sign of life it was, so a stop can tell a silent harness from
    a stuck one.
    """

    def __init__(self, started: float, hard_s: float, idle_s: float | None) -> None:
        self._started = started
        self._hard_s = hard_s
        self._idle_s = idle_s
        self._last_activity = started
        self._last_signal: Signal | None = None
        self._last_signal_at: float | None = None

    @property
    def idle_s(self) -> float | None:
        return self._idle_s

    @property
    def last_signal(self) -> Signal | None:
        """The sign of life seen last: `output`, `cpu`, `files`, or None."""
        return self._last_signal

    @property
    def last_signal_at(self) -> float | None:
        """The moment of the last sign of life."""
        return self._last_signal_at

    def touch(self, now: float, signal: Signal = "output") -> None:
        """Record a sign of life; an older moment never moves the mark back."""
        if now > self._last_activity:
            self._last_activity = now
            self._last_signal = signal
            self._last_signal_at = now

    def verdict(self, now: float) -> Verdict:
        if now - self._started >= self._hard_s:
            return "hard"
        if self._idle_s is not None and now - self._last_activity >= self._idle_s:
            return "idle"
        return "ok"
