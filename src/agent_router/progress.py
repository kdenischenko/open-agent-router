"""Live progress of a running attempt: bounded tail reads and the in-memory attempt state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_router import worktree
from agent_router.dispatcher_support import LOG_TAIL_CHARS, _tail
from agent_router.liveness import Liveness

# bytes read per log stream to fill a LOG_TAIL_CHARS tail: room for multi-byte characters
TAIL_READ_BYTES = LOG_TAIL_CHARS * 4


def read_tail(path: Path, max_bytes: int) -> str:
    """At most max_bytes from the end of the file; a missing file is an empty string.

    The window can start inside a multi-byte character or end on one still being written:
    the cut bytes are dropped instead of decoded.
    """
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - max_bytes))
            data = stream.read(max_bytes)
    except OSError:
        return ""
    while data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start == 0:
                data = data[1:]  # a character split where the window starts
                continue
            if exc.reason == "unexpected end of data":
                data = data[: exc.start]  # a character still being written at the end
                continue
            return data.decode("utf-8", "replace")
    return ""


@dataclass
class AttemptLive:
    """In-memory state of a running attempt: what the job row does not carry yet."""

    job_id: str
    n: int
    route: str
    started_at: datetime  # the dispatcher's clock, like the attempt row
    started_mono: float  # monotonic start, like the runner's liveness and hard cap
    cwd: Path
    log_dir: Path
    timeout_s: float  # the hard cap the runner enforces
    idle_timeout_s: float | None
    liveness: Liveness | None = None  # set by the runner once the process is up

    def live_tail(self) -> str:
        """The end of the attempt logs, joined like the stored tail, with paths redacted."""
        stdout = read_tail(self.log_dir / "stdout.log", TAIL_READ_BYTES)
        stderr = read_tail(self.log_dir / "stderr.log", TAIL_READ_BYTES)
        return worktree.without_paths(_tail(stdout, stderr), self.cwd)

    def attempt_json(self, now: datetime, mono: float) -> dict[str, Any]:
        """Live counters of the attempt; ages and limits use the runner's monotonic time."""
        elapsed = max(0, int((now - self.started_at).total_seconds()))
        signal: str | None = None
        signal_age_s: int | None = None
        activity = self.started_mono
        if self.liveness is not None:
            signal = self.liveness.last_signal
            at = self.liveness.last_signal_at
            signal_age_s = None if at is None else max(0, int(mono - at))
            activity = self.liveness.last_activity
        idle_left_s = None
        if self.idle_timeout_s is not None:
            idle_left_s = max(0, int(self.idle_timeout_s - (mono - activity)))
        cap_left_s = max(0, int(self.timeout_s - (mono - self.started_mono)))
        return {
            "n": self.n,
            "route": self.route,
            "elapsed_s": elapsed,
            "last_signal": signal,
            "last_signal_age_s": signal_age_s,
            "idle_left_s": idle_left_s,
            "cap_left_s": cap_left_s,
            "tail": self.live_tail(),
        }
