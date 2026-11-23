"""Signs of life of a silent harness: CPU time spent and files changed in the working tree.

A harness that prints nothing while it works looks idle to the runner. These two signals
tell a working attempt from a stuck one: the process tree burns CPU, or it writes files.
Both are best effort — when a signal cannot be read it is simply absent.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_router.winjob import WindowsJob

log = logging.getLogger(__name__)

# The runner samples the signals this often; a shorter interval would only cost file walks.
ACTIVITY_SAMPLE_S = 30.0
# CPU growth that counts as work: a hundredth of one core over the time between samples,
# so an idle runtime with timers does not look busy
CPU_SHARE = 0.01
# The walk over the working directory stops after this many files, so a huge tree
# cannot stall the poll loop.
FILE_WALK_CAP = 20000
# Directories that hold no work of the harness: their mtimes say nothing about progress.
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})

CpuReader = Callable[[], float | None]
MtimeReader = Callable[[], float | None]


def newest_mtime(cwd: Path, skip: Path | None, cap: int = FILE_WALK_CAP) -> float | None:
    """The newest mtime of the files under `cwd`; None when there is nothing to read.

    The walk is bounded by `cap` files and ignores the directories in `SKIP_DIRS` and the
    attempt log directory `skip`. Files that vanish or cannot be read are ignored.
    """
    newest: float | None = None
    seen = 0
    skip_resolved = None
    if skip is not None:
        try:
            skip_resolved = Path(skip).resolve()
        except OSError:
            skip_resolved = None
    for root, dirs, files in os.walk(cwd, topdown=True, onerror=lambda _err: None):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        try:
            resolved = Path(root).resolve()
        except OSError:
            resolved = Path(root)
        if skip_resolved is not None and resolved == skip_resolved:
            dirs[:] = []
            continue
        for name in files:
            try:
                mtime = (Path(root) / name).stat().st_mtime
            except OSError:
                continue  # the file vanished or cannot be read
            if newest is None or mtime > newest:
                newest = mtime
            seen += 1
            if seen >= cap:
                return newest
    return newest


def _process_group_cpu_time_s(pid: int) -> float | None:
    """CPU seconds of the processes in the group of `pid`; None when it cannot be read."""
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:  # no process groups away from POSIX
        return None
    try:
        group = getpgid(pid)
    except OSError:
        return None
    total = 0.0
    found = False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        close = content.rfind(")")
        if close < 0:
            continue
        fields = content[close + 2 :].split()
        if len(fields) < 13:
            continue
        try:
            if int(fields[2]) != group:  # the process group field
                continue
            ticks = int(fields[11]) + int(fields[12])  # utime and stime
        except ValueError:
            continue
        found = True
        total += ticks / os.sysconf("SC_CLK_TCK")
    return total if found else None


def posix_cpu_time_s(pid: int) -> float | None:
    """CPU seconds of the process tree of `pid` on Linux; None when it cannot be read."""
    if sys.platform != "linux":
        return None
    try:
        return _process_group_cpu_time_s(pid)
    except (OSError, ValueError) as exc:  # a broken /proc: the signal is simply absent
        log.debug("cpu time not read: %s", exc)
        return None


class ActivitySampler:
    """Samples the signs of life of one attempt and reports which one it saw.

    The readers are injected, so the class can be tested without a real process tree and
    without a real clock: `sample` only compares against the previous sample.
    """

    def __init__(
        self,
        cwd: Path,
        cpu_reader: CpuReader | None = None,
        mtime_reader: MtimeReader | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._cpu_reader = cpu_reader
        self._mtime_reader = mtime_reader
        self._cpu_base: float | None = None
        self._cpu_base_at: float | None = None
        self._mtime_base: float | None = None

    def sample(self, now: float) -> str | None:
        """The signal seen since the previous sample, or None when nothing moved."""
        signal = None
        cpu = self._read(self._cpu_reader)
        if cpu is not None:
            if self._cpu_base is not None and self._cpu_base_at is not None:
                threshold = CPU_SHARE * max(0.0, now - self._cpu_base_at)
                if cpu - self._cpu_base >= threshold and cpu > self._cpu_base:
                    signal = "cpu"
            self._cpu_base = cpu
            self._cpu_base_at = now
        mtime = self._read(self._mtime_reader)
        if mtime is not None:
            if self._mtime_base is not None and mtime > self._mtime_base:
                signal = signal or "files"
            self._mtime_base = mtime
        return signal

    @staticmethod
    def _read(reader: Callable[[], float | None] | None) -> float | None:
        """One reading; a failing reader gives no value instead of an exception."""
        if reader is None:
            return None
        try:
            return reader()
        except (OSError, ValueError) as exc:
            log.debug("activity signal not read: %s", exc)
            return None


def cpu_reader_for(job: WindowsJob | None, pid: int) -> CpuReader | None:
    """A reader of the CPU time of the attempt's process tree; None when there is none.

    On Windows the job object knows the CPU time of every process that ran in the attempt,
    including the ones that already exited. On Linux the process group is read from /proc.
    """
    reader = getattr(job, "cpu_time_s", None)
    if callable(reader):
        return reader
    if sys.platform == "linux":
        return lambda: posix_cpu_time_s(pid)
    return None
