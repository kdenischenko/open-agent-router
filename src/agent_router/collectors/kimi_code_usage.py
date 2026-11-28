"""Kimi Code plan usage: window quotas read from the local server of the Kimi Code CLI.

The CLI runs with the user's own sign-in (`kimi login`). The source never reads Kimi Code
credentials: it starts the unmodified CLI's local server (`kimi web --json --port 0
--log-level silent`), which prints one ready line with an ephemeral bearer token, and reads
`GET /api/v1/oauth/usage` with it. The token lives in a local variable for one read and is
never logged; a read asks the server for the plan quota and does not call a model.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx

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

DEFAULT_EXECUTABLE = "kimi"
TIMEOUT_S = 60.0
MIN_VERSION = "2.8.1"  # `web --json` first shipped here; older CLIs reject the option
USAGE_ARGS = ("web", "--json", "--port", "0", "--log-level", "silent")
USAGE_PATH = "/api/v1/oauth/usage"
FIELDS = ("limit5h", "limit7d", "monthTotal", "monthCode")

# on_ready gets the origin, the ephemeral token and the seconds left of the read budget; its
# UsageRead is the read's result. The runner returns (failure, read); exactly one is not None.
ReadyCallback = Callable[[str, str, float], UsageRead]
ServerRunner = Callable[
    [Sequence[str], Mapping[str, str], float, ReadyCallback],
    tuple[str | None, UsageRead | None],
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


def _ready(line: str) -> tuple[str, str] | None:
    """Origin and token of the started server from its ready line; None when it is not one."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, Mapping):
        return None
    origin = message.get("origin")
    token = message.get("token")
    if not isinstance(origin, str) or not origin.startswith("http"):
        return None
    if not isinstance(token, str) or not token:
        return None
    return origin, token


def run_server(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout_s: float,
    on_ready: ReadyCallback,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> tuple[str | None, UsageRead | None]:
    """Start `kimi web --json`, wait for its ready line, hand it to on_ready, stop the tree.

    The ready line carries the server's ephemeral token, so it is never logged or stored: a
    line that is not a valid ready line ends the read as "malformed_response" without the
    response-shape log. The process tree is stopped on every path — success, failure,
    timeout and exception alike.
    """
    try:
        check_shim_args(argv)
        proc = popen(  # no shell, arguments passed as a list
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            **_popen_options(),
        )
    except (OSError, ShimUnsafeArgs):
        return "not_found", None
    try:
        lines: queue.Queue[bytes | None] = queue.Queue()
        errors = bytearray()

        def pump_stdout() -> None:
            assert proc.stdout is not None
            with contextlib.suppress(OSError, ValueError):
                for raw in proc.stdout:
                    lines.put(raw)
            lines.put(None)

        def pump_stderr() -> None:
            assert proc.stderr is not None
            with contextlib.suppress(OSError, ValueError):
                for raw in proc.stderr:
                    errors.extend(raw)

        stderr_pump = threading.Thread(target=pump_stderr, name="kimi-usage-stderr", daemon=True)
        threading.Thread(target=pump_stdout, name="kimi-usage-stdout", daemon=True).start()
        stderr_pump.start()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            try:
                raw = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                break  # the process ended without a ready line
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if "unknown option" in line:
                # a CLI without `web --json` rejects the option and exits at once
                return "unsupported_version", None
            if not line.startswith("{"):
                continue  # a plain-text notice before the ready line
            ready = _ready(line)
            if ready is None:
                # not logged: the line comes from the stream that carries the token
                return "malformed_response", None
            origin, token = ready
            try:
                return None, on_ready(origin, token, max(deadline - time.monotonic(), 0.1))
            except Exception:
                return "error", None
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc.wait(timeout=KILL_WAIT_S)
        stderr_pump.join(1.0)  # let the pump drain the ended process's stderr
        if "unknown option" in errors.decode("utf-8", "replace"):
            return "unsupported_version", None
        return "error", None
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if proc.poll() is None:
                _stop(proc)


def _reset_time(value: object) -> datetime | None:
    """UTC reset time from an ISO 8601 string; a malformed value raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reset time is not a string")
    return to_utc(datetime.fromisoformat(value))


def _point(field: str, meter: object) -> UsagePoint | None:
    """One quota meter as a point; a bad ratio or reset time skips the meter."""
    if not isinstance(meter, Mapping):
        return None
    ratio = meter.get("usedRatio")
    if isinstance(ratio, bool) or not isinstance(ratio, int | float):
        return None
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        return None
    try:
        resets_at = _reset_time(meter.get("resetAt"))
    except ValueError:
        return None
    return UsagePoint(field=field, fraction=float(ratio), resets_at=resets_at)


def _points(usages: object) -> tuple[UsagePoint, ...]:
    """Usable meters of the quota's `usages` map; a missing or malformed meter is skipped."""
    if not isinstance(usages, Mapping):
        return ()
    return tuple(
        point for field in FIELDS if (point := _point(field, usages.get(field))) is not None
    )


def _read_body(body: object, raw: bytes) -> UsageRead:
    """The usage envelope as a read; an account-side error maps to a reason code."""
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping):
        log_malformed_response(SOURCE.name, raw)
        return UsageRead(reason="malformed_response")
    if data.get("kind") == "error":
        # 401: the CLI is not signed in; 404: the account has no managed Kimi Code quota
        return UsageRead(reason="unauthorized" if data.get("status") == 401 else "error")
    quota = data.get("quota")
    usages = quota.get("usages") if isinstance(quota, Mapping) else None
    points = _points(usages)
    if not points:
        log_malformed_response(SOURCE.name, raw)
        return UsageRead(reason="malformed_response")
    return UsageRead(points=points)


class KimiCodeUsage:
    """Reads the plan's 5-hour and monthly quota meters from the Kimi Code local server.

    The server token from the ready line is a secret: it lives only in a local variable of
    one read, and no reason, warning, exception or log ever contains it.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        executable: str = DEFAULT_EXECUTABLE,
        run: ServerRunner = run_server,
        resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
        client: httpx.Client | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._env = env
        self._executable = executable
        self._run = run
        self._resolve = resolve
        self._client = client  # injected in tests; otherwise one is created per read
        self._timeout_s = timeout_s

    def read(self) -> UsageRead:
        resolved = self._resolve(self._executable)
        if resolved.path is None:
            return UsageRead(reason="not_found")
        failure, read = self._run(
            [str(resolved.path), *USAGE_ARGS], self._env, self._timeout_s, self._read_usage
        )
        if read is not None:
            return read
        return UsageRead(reason=failure or "error")

    def _read_usage(self, origin: str, token: str, timeout_s: float) -> UsageRead:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            response = self._get(origin, headers, timeout_s)
        # the caught errors are never logged: they carry the request and its authorization header
        except httpx.TimeoutException:
            return UsageRead(reason="timeout")
        except (httpx.HTTPError, httpx.InvalidURL):  # InvalidURL is not an HTTPError
            return UsageRead(reason="error")
        if response.status_code == 401:  # our server token was rejected, not the account's
            return UsageRead(reason="unauthorized")
        if not response.is_success:
            return UsageRead(reason="error")
        try:
            body = response.json()
        except ValueError:  # includes json.JSONDecodeError
            log_malformed_response(SOURCE.name, response.content)
            return UsageRead(reason="malformed_response")
        return _read_body(body, response.content)

    def _get(self, origin: str, headers: Mapping[str, str], timeout_s: float) -> httpx.Response:
        url = origin.rstrip("/") + USAGE_PATH
        if self._client is not None:
            return self._client.get(url, headers=headers, timeout=timeout_s)
        # loopback only: the environment proxy must not see the ephemeral token
        with httpx.Client(timeout=timeout_s, trust_env=False) as client:
            return client.get(url, headers=headers)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)+", text)
    return tuple(int(part) for part in match.group().split(".")) if match else None


def check(
    pool_usage: PoolUsage,
    env: Mapping[str, str],
    command: CommandRunner = run_command,
    resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
) -> str | None:
    """Doctor's warning for a missing or too old CLI; None when ready. Never starts the server."""
    executable = pool_usage.executable or DEFAULT_EXECUTABLE
    resolved = resolve(executable)
    if resolved.path is None:
        return f"cannot find the Kimi Code CLI {executable!r}; install it or set executable"
    output = command([str(resolved.path), "--version"], env, TIMEOUT_S)
    if output is None:
        return None  # the version could not be read; the read itself will report a reason
    found = _version_tuple(output)
    if found is not None and found < _version_tuple(MIN_VERSION):
        return f"Kimi Code {MIN_VERSION} or newer is needed for usage reads; run kimi upgrade"
    return None


def _factory(pool_usage: PoolUsage, env: Mapping[str, str]) -> KimiCodeUsage:
    return KimiCodeUsage(env, pool_usage.executable or DEFAULT_EXECUTABLE)


SOURCE = UsageSourceSpec(
    name="kimi-code",
    factory=_factory,
    windows=frozenset(FIELDS),
    observation_source="kimi_code_usage",
    check=check,
)
