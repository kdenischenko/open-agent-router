"""ChatGPT plan usage: window meters read from the Codex CLI app-server.

The CLI runs with the user's own sign-in (`codex login`). The source never reads Codex
credentials and never passes a token: it starts the unmodified CLI, initializes its JSON-RPC
app-server and reads the account's rate limits without calling a model.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_router import __version__
from agent_router.collectors.response_shape import log_malformed_response
from agent_router.config_model import PoolUsage
from agent_router.runner import (
    KILL_WAIT_S,
    ResolvedExecutable,
    ShimUnsafeArgs,
    check_shim_args,
    kill_tree,
    resolve_executable,
)
from agent_router.usage import UsagePoint, UsageRead, UsageSourceSpec

DEFAULT_EXECUTABLE = "codex"
TIMEOUT_S = 60.0
USAGE_ARGS = ("app-server",)
STATUS_ARGS = ("login", "status")
KINDS = frozenset({"primary", "secondary"})
REQUESTS = tuple(
    json.dumps(message, separators=(",", ":"))
    for message in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "agent-router", "version": __version__}},
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
    )
)

# on_line returns False to stop the process; the result is None when the stream ended on its own,
# otherwise a reason code ("not_found", "timeout", "error")
StreamRunner = Callable[
    [Sequence[str], Mapping[str, str], float, Callable[[str], bool]], str | None
]
# stdout and stderr of a short command, or None when it could not run or timed out
CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], str | None]


def _popen_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}  # own process group so the tree can be killed


def _stop(proc: subprocess.Popen[bytes]) -> None:
    kill_tree(proc.pid)
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=KILL_WAIT_S)


def run_stream(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout_s: float,
    on_line: Callable[[str], bool],
) -> str | None:
    """Run the app-server, write its requests and feed stdout lines to on_line."""
    try:
        check_shim_args(argv)
        proc = subprocess.Popen(  # no shell, arguments passed as a list
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            **_popen_options(),
        )
    except (OSError, ShimUnsafeArgs):
        return "not_found"
    try:
        assert proc.stdin is not None
        try:
            # stdin stays open: the app-server quits on end of input and drops pending requests
            proc.stdin.write(("\n".join(REQUESTS) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            return "error"
        lines: queue.Queue[bytes | None] = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            with contextlib.suppress(OSError, ValueError):
                for raw in proc.stdout:
                    lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, name="codex-usage-pump", daemon=True).start()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop(proc)
                return "timeout"
            try:
                raw = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                break
            if not on_line(raw.decode("utf-8", "replace")):
                _stop(proc)
                return None
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            _stop(proc)
            return "timeout"
        return None
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if proc.poll() is None:
                _stop(proc)
        with contextlib.suppress(OSError):
            proc.stdin.close()


def run_command(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> str | None:
    """Combined stdout and stderr of a short command, whatever its exit code."""
    try:
        check_shim_args(argv)
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            **_popen_options(),
        )
    except (OSError, ShimUnsafeArgs):
        return None
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop(proc)
            return None
        return (stdout + b"\n" + stderr).decode("utf-8", "replace")
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if proc.poll() is None:
                _stop(proc)


def _reset_time(value: object) -> datetime | None:
    """UTC reset time from Unix seconds; a malformed value raises ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("reset time is not a number")
    if not math.isfinite(value):
        raise ValueError("reset time is not finite")
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("reset time is outside the supported range") from exc


def _point(kind: str, row: object) -> UsagePoint | None:
    """One rate-limit meter as a point; a bad percent or reset time skips it."""
    if not isinstance(row, Mapping):
        return None
    percent = row.get("usedPercent")
    if isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        return None
    try:
        resets_at = _reset_time(row.get("resetsAt"))
    except ValueError:
        return None
    return UsagePoint(field=kind, fraction=percent / 100, resets_at=resets_at)


def _points(limits: object) -> tuple[UsagePoint, ...]:
    if not isinstance(limits, Mapping):
        return ()
    return tuple(
        point for kind in ("primary", "secondary") if (point := _point(kind, limits.get(kind)))
    )


@dataclass
class _Stream:
    """The app-server output up to the account/rateLimits/read response."""

    found: bool = False
    response: Mapping[str, Any] | None = None
    raw: bytearray = field(default_factory=bytearray)

    def feed(self, line: str) -> bool:
        """Take one output line; False stops the process after response id 2."""
        self.raw.extend(line.encode("utf-8"))
        try:
            event = json.loads(line)
        except ValueError:
            return True
        if not isinstance(event, Mapping) or event.get("id") != 2:
            return True
        self.found = True
        self.response = event
        return False


def _auth_error(error: object) -> bool:
    if not isinstance(error, Mapping):
        return False
    message = str(error.get("message", "")).lower()
    return any(part in message for part in ("auth", "login", "log in", "sign in", "signin"))


def _signed_in(status: str | None) -> bool | None:
    if status is None:
        return None
    lowered = status.lower()
    if "not logged in" in lowered:
        return False
    if "logged in" in lowered:
        return True
    return None


def _process_warning(failure: str | None) -> str | None:
    if failure is None:
        return None
    messages = {
        "timeout": "the Codex CLI timed out after returning rate limits",
        "not_found": "the Codex CLI was not found while reading usage",
    }
    return messages.get(failure, "the Codex CLI failed after returning rate limits")


class CodexUsage:
    """Reads the five-hour and weekly meters of a ChatGPT plan from Codex app-server."""

    def __init__(
        self,
        env: Mapping[str, str],
        executable: str = DEFAULT_EXECUTABLE,
        run: StreamRunner = run_stream,
        command: CommandRunner = run_command,
        resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._env = env
        self._executable = executable
        self._run = run
        self._command = command
        self._resolve = resolve
        self._timeout_s = timeout_s

    def read(self) -> UsageRead:
        resolved = self._resolve(self._executable)
        if resolved.path is None:
            return UsageRead(reason="not_found")
        cli = str(resolved.path)
        stream = _Stream()
        failure = self._run([cli, *USAGE_ARGS], self._env, self._timeout_s, stream.feed)
        if not stream.found or stream.response is None:
            if failure in {"not_found", "timeout", "error"}:
                return UsageRead(reason=failure)
            log_malformed_response(SOURCE.name, bytes(stream.raw), json_lines=True)
            return UsageRead(reason="malformed_response")
        error = stream.response.get("error")
        if error is not None:
            return UsageRead(reason="unauthorized" if _auth_error(error) else "error")
        result = stream.response.get("result")
        limits = result.get("rateLimits") if isinstance(result, Mapping) else None
        points = _points(limits)
        if points:
            return UsageRead(points=points, warning=_process_warning(failure))
        if not isinstance(limits, Mapping):
            status = self._command([cli, *STATUS_ARGS], self._env, self._timeout_s)
            if _signed_in(status) is False:
                return UsageRead(reason="unauthorized")
        log_malformed_response(SOURCE.name, bytes(stream.raw), json_lines=True)
        return UsageRead(reason="malformed_response")


def check(
    pool_usage: PoolUsage,
    env: Mapping[str, str],
    command: CommandRunner = run_command,
    resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
) -> str | None:
    """Doctor's warning for a missing CLI or a CLI that is not signed in; None when ready."""
    executable = pool_usage.executable or DEFAULT_EXECUTABLE
    resolved = resolve(executable)
    if resolved.path is None:
        return f"cannot find the Codex CLI {executable!r}; install it or set executable"
    status = command([str(resolved.path), *STATUS_ARGS], env, TIMEOUT_S)
    if _signed_in(status) is not True:
        return "the Codex CLI is not signed in; run codex login"
    return None


def _factory(pool_usage: PoolUsage, env: Mapping[str, str]) -> CodexUsage:
    return CodexUsage(env, pool_usage.executable or DEFAULT_EXECUTABLE)


SOURCE = UsageSourceSpec(
    name="codex",
    factory=_factory,
    windows=KINDS,
    observation_source="codex_usage",
    check=check,
)
