"""Claude plan usage: window meters read from the `/usage` command of the Claude Code CLI.

The CLI runs in print mode with the user's own sign-in (`claude auth login`). The source never
reads the Claude Code credentials and never passes a token: it starts the unmodified CLI and
reads the structured `usage_report` the CLI attaches to the synthetic `/usage` message.
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
from datetime import datetime
from typing import Any

from agent_router.clock import to_utc
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

DEFAULT_EXECUTABLE = "claude"
TIMEOUT_S = 60.0
SYNTHETIC_MODEL = "<synthetic>"
KINDS = frozenset({"session", "weekly_all"})
# no user settings, hooks, plugins or MCP servers: the gateway needs the command and nothing else
USAGE_ARGS = (
    "-p",
    "/usage",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
)
STATUS_ARGS = ("auth", "status", "--json")

# on_line returns False to stop the process; the result is None when the stream ended on its own,
# otherwise a reason code ("not_found", "timeout")
StreamRunner = Callable[
    [Sequence[str], Mapping[str, str], float, Callable[[str], bool]], str | None
]
# stdout of a short command, or None when it could not run or timed out
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
    """Run argv without a shell and feed each stdout line to on_line until it returns False."""
    try:
        check_shim_args(argv)
        proc = subprocess.Popen(  # no shell, arguments passed as a list
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            **_popen_options(),
        )
    except (OSError, ShimUnsafeArgs):
        return "not_found"
    lines: queue.Queue[bytes | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        with contextlib.suppress(OSError, ValueError):
            for raw in proc.stdout:
                lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, name="claude-usage-pump", daemon=True).start()
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


def run_command(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> str | None:
    """Stdout of a short command run without a shell, whatever its exit code."""
    try:
        check_shim_args(argv)
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            **_popen_options(),
        )
    except (OSError, ShimUnsafeArgs):
        return None
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return None
    return stdout.decode("utf-8", "replace")


def _reset_time(value: object) -> datetime | None:
    """UTC reset time from an ISO 8601 string; a malformed value raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reset time is not a string")
    return to_utc(datetime.fromisoformat(value))


def _point(row: object) -> UsagePoint | None:
    """One limits row as a point; an unknown kind, bad percent or bad reset time skips it."""
    if not isinstance(row, Mapping):
        return None
    kind = row.get("kind")
    if kind not in KINDS:
        return None
    percent = row.get("percent")
    if isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        return None
    try:
        resets_at = _reset_time(row.get("resets_at"))
    except ValueError:
        return None
    return UsagePoint(field=str(kind), fraction=percent / 100, resets_at=resets_at)


def _text(message: object) -> str:
    """Plain text of an assistant message's content blocks."""
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in message["content"]
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(parts)


@dataclass
class _Stream:
    """What the `/usage` stream said so far."""

    found: bool = False  # the synthetic message of the usage command was seen
    report: object = None  # its usage_report, None when absent
    text: str = ""  # its text, plus the result text
    model_called: bool = False
    raw: bytearray = field(default_factory=bytearray)

    def feed(self, line: str) -> bool:
        """Take one stream line; False stops the process."""
        self.raw.extend(line.encode("utf-8"))
        try:
            event = json.loads(line)
        except ValueError:
            return True  # a line that is not JSON says nothing about usage
        if not isinstance(event, Mapping):
            return True
        if event.get("type") == "result":
            if isinstance(event.get("result"), str):
                self.text += "\n" + event["result"]
            return True
        if event.get("type") != "assistant":
            return True
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("model") != SYNTHETIC_MODEL:
            self.model_called = True
            return False
        command = event.get("local_command_run")
        if isinstance(command, Mapping) and command.get("command") == "usage":
            self.found = True
            self.report = event.get("usage_report")
            self.text += "\n" + _text(message)
        return True


def _points(report: Mapping[str, Any]) -> tuple[UsagePoint, ...]:
    limits = report.get("rate_limits")
    rows = limits.get("limits") if isinstance(limits, Mapping) else None
    if not isinstance(rows, list):
        return ()
    return tuple(point for point in map(_point, rows) if point is not None)


def _signed_in(status: str | None) -> bool | None:
    """loggedIn from `claude auth status --json`; None when the answer is unusable."""
    if status is None:
        return None
    try:
        body = json.loads(status)
    except ValueError:
        return None
    if not isinstance(body, Mapping) or not isinstance(body.get("loggedIn"), bool):
        return None
    return body["loggedIn"]


def _process_warning(failure: str | None) -> str | None:
    if failure is None:
        return None
    messages = {
        "timeout": "the Claude Code CLI timed out after returning a usage report",
        "not_found": "the Claude Code CLI was not found while reading usage",
    }
    return messages.get(failure, "the Claude Code CLI failed after returning a usage report")


class ClaudeCodeUsage:
    """Reads the session and weekly meters of a Claude plan from `claude -p /usage`."""

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
        if stream.model_called:
            log_malformed_response(SOURCE.name, bytes(stream.raw), json_lines=True)
            return UsageRead(reason="malformed_response")
        report = stream.report if isinstance(stream.report, Mapping) else None
        if report is not None:
            if report.get("rate_limits_available") is False:
                return UsageRead(reason="unauthorized")
            points = _points(report)
            if points:
                return UsageRead(points=points, warning=_process_warning(failure))
        if failure is not None:
            return UsageRead(reason=failure)
        lowered = stream.text.lower()
        if "rate limit" in lowered or "rate-limit" in lowered:
            return UsageRead(reason="rate_limited")
        if report is None:
            # a signed-out CLI prints only the session cost, without a usage_report
            status = self._command([cli, *STATUS_ARGS], self._env, self._timeout_s)
            if _signed_in(status) is False or "not logged in" in lowered:
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
        return f"cannot find the Claude Code CLI {executable!r}; install it or set executable"
    status = command([str(resolved.path), *STATUS_ARGS], env, TIMEOUT_S)
    signed_in = _signed_in(status)
    if signed_in is False:
        return "the Claude Code CLI is not signed in; run claude auth login"
    if signed_in is None:
        return "the Claude Code CLI did not report its sign-in status"
    return None


def _factory(pool_usage: PoolUsage, env: Mapping[str, str]) -> ClaudeCodeUsage:
    return ClaudeCodeUsage(env, pool_usage.executable or DEFAULT_EXECUTABLE)


SOURCE = UsageSourceSpec(
    name="claude-code",
    factory=_factory,
    windows=KINDS,
    observation_source="claude_code_usage",
    check=check,
)
