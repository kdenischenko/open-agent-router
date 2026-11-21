"""Executor launch: a blocking process in a worker thread."""

from __future__ import annotations

import calendar
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

SHIM_SUFFIXES = (".cmd", ".bat")
_SHIM_SAFE = frozenset(" ._:/\\=@+-")
_TRUNCATED_MARK = b"\n[agent-router: output truncated]\n"
# Two queries in on_start (image and creation time), tree termination, waits,
# and two reader threads.
STOP_BUDGET_S = 35.0
PROC_START_TOLERANCE_S = 1.0
_POLL_S = 0.05
QUERY_WAIT_S = 5.0
KILL_WAIT_S = 5.0
_READER_WAIT_S = 5.0
_LINE_BUFFER_MAX = 2**20
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin_text: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout_path: Path
    stderr_path: Path
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    forced_after_final: bool = False
    survived: bool = False  # process did not exit after the tree was stopped


@dataclass(frozen=True)
class ResolvedExecutable:
    path: Path | None
    is_shim: bool  # a .cmd or .bat shim: cmd.exe parses its arguments


class ProcessQueryError(Exception):
    """Process query failed: no answer, or a malformed one (this is not "no such process")."""


class ShimUnsafeArgs(Exception):
    """A .cmd/.bat shim argument has characters the command interpreter would parse."""


def is_shim(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in SHIM_SUFFIXES


def resolve_executable(name_or_path: str) -> ResolvedExecutable:
    """Resolve a PATH name or path to the real file; None if it does not exist."""
    found: str | None
    candidate = Path(name_or_path).expanduser()
    candidate_text = str(candidate)
    if candidate.is_absolute() or os.sep in candidate_text or "/" in candidate_text:
        found = candidate_text if candidate.is_file() else shutil.which(candidate_text)
    else:
        found = shutil.which(candidate_text)
    if found is None:
        return ResolvedExecutable(None, False)
    path = Path(os.path.abspath(found))  # Popen would resolve a relative path from the job's cwd
    return ResolvedExecutable(path, is_shim(path))


def check_shim_args(argv: Sequence[str]) -> None:
    """For a .cmd/.bat shim, argv[1:] allows only letters, digits, space, and . _ : / \\ = @ + -."""
    if not argv or not is_shim(argv[0]):
        return
    for arg in argv[1:]:
        for char in arg:
            if not (char.isalnum() or char in _SHIM_SAFE):
                raise ShimUnsafeArgs(
                    f"shim argument contains an unsafe character {char!r}: launch rejected"
                )


def _run_quiet(
    argv: Sequence[str], timeout: float = 30, env: Mapping[str, str] | None = None
) -> str | None:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace")


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid: int) -> None:
    """Terminate the process tree: taskkill /T /F on Windows; killpg with SIGTERM then SIGKILL."""
    if sys.platform == "win32":
        _run_quiet(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=KILL_WAIT_S)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_WAIT_S
    while time.monotonic() < deadline and _group_alive(pid):
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)  # our own child: reap the zombie
        time.sleep(_POLL_S)
    if _group_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def _survived(pid: int, parent_alive: bool) -> bool:
    """Whether the process outlived the stop; on POSIX, finishes a live group whose parent died."""
    if parent_alive:
        return True
    if sys.platform == "win32":
        return False  # children of a dead parent are not visible on Windows
    if not _group_alive(pid):
        return False
    kill_tree(pid)
    return _group_alive(pid)


def _run_query(
    argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    """Query exit code, stdout, and stderr; launch failure or timeout → ProcessQueryError."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessQueryError(f"process query failed: {exc}") from exc
    return (
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        (done.stderr or b"").decode("utf-8", "replace"),
    )


def process_image(pid: int) -> str | None:
    """Image name of a live process; None if absent; query failure → ProcessQueryError."""
    if sys.platform == "win32":
        code, out, _err = _run_query(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
            timeout=QUERY_WAIT_S,
        )
        if code != 0:
            raise ProcessQueryError(f"process query exited with code {code}")
        for line in out.splitlines():
            parts = [part.strip('"') for part in line.strip().split('","')]
            if len(parts) >= 2 and parts[1] == str(int(pid)):
                return parts[0] or None
        return None
    # A valid screen size keeps procps from warning about a bogus size on stderr, and the
    # wide value keeps long macOS command names from being truncated
    env = dict(os.environ, LINES="24", COLUMNS="4096")
    code, out, err = _run_query(
        ["ps", "-p", str(int(pid)), "-o", "stat=,comm="], timeout=QUERY_WAIT_S, env=env
    )
    if code == 1 and not out and not err.strip():
        return None  # PID not found; text in stderr means ps itself failed
    if code != 0:
        raise ProcessQueryError(f"process query exited with code {code}")
    parts = out.split(maxsplit=1)
    if len(parts) != 2 or parts[0].startswith("Z"):
        return None
    return parts[1].strip() or None


def _filetime_to_epoch(low: int, high: int) -> float:
    return ((high << 32) | low) / 1e7 - 11644473600


def process_started(pid: int) -> float | None:
    """Creation time of a live process in epoch seconds; None if unavailable."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            get_times = kernel32.GetProcessTimes
            filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
            get_times.argtypes = [
                wintypes.HANDLE,
                filetime_pointer,
                filetime_pointer,
                filetime_pointer,
                filetime_pointer,
            ]
            get_times.restype = wintypes.BOOL
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            handle = open_process(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                    return None
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not get_times(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    return None
                return _filetime_to_epoch(creation.dwLowDateTime, creation.dwHighDateTime)
            finally:
                close_handle(handle)
        except (OSError, ValueError, OverflowError, ctypes.ArgumentError):
            return None
    # ps prints time in the TZ zone: UTC0 drops the local zone and daylight-saving shifts
    env = dict(os.environ, LC_ALL="C", TZ="UTC0")
    out = _run_quiet(["ps", "-p", str(int(pid)), "-o", "lstart="], timeout=QUERY_WAIT_S, env=env)
    parts = (out or "").split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        return None
    try:
        hour, minute, second = (int(part) for part in parts[3].split(":"))
        return calendar.timegm(
            (int(parts[4]), _MONTHS[parts[1]], int(parts[2]), hour, minute, second)
        )
    except (OSError, OverflowError, ValueError):
        return None


class Runner(Protocol):
    id: str

    def run(
        self,
        inv: Invocation,
        cwd: Path,
        timeout_s: float,
        log_dir: Path,
        cancel: threading.Event,
        is_final_line: Callable[[str], bool] | None = None,
        exit_grace_s: float = 60,
        on_start: Callable[[int], None] | None = None,
    ) -> ProcessResult: ...


class _Pump(threading.Thread):
    """Pump a process stream into a file; past the limit it truncates but drains the pipe."""

    def __init__(
        self,
        stream: IO[bytes],
        path: Path,
        limit: int,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(daemon=True, name=f"runner-pump-{path.name}")
        self._stream = stream
        self._path = path
        self._limit = limit
        self._on_line = on_line
        self.truncated = False

    def run(self) -> None:
        written = 0
        pending = b""
        with contextlib.suppress(OSError, ValueError), self._path.open("wb", buffering=0) as out:
            while True:
                chunk = self._stream.read1(65536)  # type: ignore[attr-defined]
                if not chunk:
                    break
                if not self.truncated:
                    room = self._limit - written
                    if len(chunk) > room:
                        out.write(chunk[:room])
                        out.write(_TRUNCATED_MARK)
                        self.truncated = True
                    else:
                        out.write(chunk)
                        written += len(chunk)
                if self._on_line is not None:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        self._on_line(line.decode("utf-8", "replace").rstrip("\r"))
                    if len(pending) > _LINE_BUFFER_MAX:
                        pending = b""
            if self._on_line is not None and pending:
                self._on_line(pending.decode("utf-8", "replace").rstrip("\r"))
        with contextlib.suppress(OSError, ValueError):
            self._stream.close()


def _feed(stream: IO[bytes], text: str) -> None:
    """Write the prompt to the process stdin and close the stream right away."""
    with contextlib.suppress(OSError, ValueError):
        stream.write(text.encode("utf-8"))
    with contextlib.suppress(OSError, ValueError):
        stream.close()


class LocalRunner:
    """A process on this machine: no shell, stdin closed, output to files in the job directory."""

    id = "local"
    max_stream_bytes = 20 * 2**20

    def run(
        self,
        inv: Invocation,
        cwd: Path,
        timeout_s: float,
        log_dir: Path,
        cancel: threading.Event,
        is_final_line: Callable[[str], bool] | None = None,
        exit_grace_s: float = 60,
        on_start: Callable[[int], None] | None = None,
    ) -> ProcessResult:
        check_shim_args(inv.argv)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"
        started = time.monotonic()
        if cancel.is_set():
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")
            return ProcessResult(
                exit_code=None,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_s=time.monotonic() - started,
                cancelled=True,
            )
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        # opencode run takes the project directory from PWD, not from the process cwd; the
        # gateway's inherited PWD would send the job record where the gateway started
        env["PWD"] = str(cwd)
        env.update(inv.env)
        options: dict[str, object] = {}
        if sys.platform == "win32":
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            options["start_new_session"] = True  # its own group: the whole tree terminates

        final_at: list[float] = []

        def on_line(line: str) -> None:
            if not final_at and is_final_line is not None and is_final_line(line):
                final_at.append(time.monotonic())

        proc = subprocess.Popen(  # no shell, arguments passed as a list
            list(inv.argv),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if inv.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **options,  # type: ignore[arg-type]
        )
        out_pump: _Pump | None = None
        err_pump: _Pump | None = None
        try:
            if on_start is not None:
                on_start(proc.pid)
            assert proc.stdout is not None and proc.stderr is not None
            out_pump = _Pump(
                proc.stdout,
                stdout_path,
                self.max_stream_bytes,
                on_line if is_final_line is not None else None,
            )
            err_pump = _Pump(proc.stderr, stderr_path, self.max_stream_bytes)
            out_pump.start()
            err_pump.start()
            if inv.stdin_text is not None:
                assert proc.stdin is not None
                threading.Thread(
                    target=_feed,
                    args=(proc.stdin, inv.stdin_text),
                    daemon=True,
                    name="runner-stdin",
                ).start()

            deadline = started + max(0.0, float(timeout_s))
            timed_out = cancelled = forced = False
            while proc.poll() is None:
                now = time.monotonic()
                if cancel.is_set():
                    cancelled = True
                elif now >= deadline:
                    timed_out = True
                elif final_at and now - final_at[0] >= exit_grace_s:
                    forced = True  # known harness hang after the final line
                if cancelled or timed_out or forced:
                    self._kill(proc)
                    break
                time.sleep(_POLL_S)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=KILL_WAIT_S)
            out_pump.join(_READER_WAIT_S)
            err_pump.join(_READER_WAIT_S)
            parent_alive = proc.poll() is None
            survived = _survived(proc.pid, parent_alive)
            exit_code = None if parent_alive else proc.returncode
            if forced and not parent_alive:
                exit_code = 0
            return ProcessResult(
                exit_code=exit_code,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_s=time.monotonic() - started,
                timed_out=timed_out,
                cancelled=cancelled,
                truncated=out_pump.truncated or err_pump.truncated,
                forced_after_final=forced,
                survived=survived,
            )
        except BaseException:  # after cleanup the exception propagates
            with contextlib.suppress(Exception):
                self._kill(proc)
            with contextlib.suppress(Exception):
                proc.wait(timeout=KILL_WAIT_S)
            if proc.stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.close()
            # A started reader closes its own pipe; closing a pipe that is still being read
            # would wait for a process that survived the stop — only pipes without a reader close.
            for pump, stream in ((out_pump, proc.stdout), (err_pump, proc.stderr)):
                if pump is not None and pump.ident is not None:
                    with contextlib.suppress(Exception):
                        pump.join(_READER_WAIT_S)
                elif stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
            raise

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        if sys.platform == "win32":
            kill_tree(proc.pid)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=KILL_WAIT_S)
        # the parent may have exited on SIGTERM while other processes in the group did not
        if proc.poll() is None or _group_alive(proc.pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
