"""Kimi Code usage source: local-server reads, reason codes, token secrecy and doctor check."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_router import registry
from agent_router.collectors import kimi_code_usage
from agent_router.collectors.kimi_code_usage import (
    MIN_VERSION,
    SOURCE,
    USAGE_ARGS,
    USAGE_PATH,
    KimiCodeUsage,
    check,
    run_server,
)
from agent_router.config import load_config
from agent_router.config_model import PoolUsage
from agent_router.runner import ResolvedExecutable
from agent_router.usage import UsagePoint, UsageRead

CLI = Path("C:/tools/kimi.cmd") if sys.platform == "win32" else Path("/opt/kimi/bin/kimi")
ORIGIN = "http://127.0.0.1:57616"
TOKEN = "ephemeral-server-token-sentinel-835291"
READY_LINE = json.dumps(
    {
        "origin": ORIGIN,
        "ws_url": "ws://127.0.0.1:57616/ws",
        "token": TOKEN,
        "kimi_agent_protocol_version": "1.0.0",
    }
)

OK_BODY = json.dumps(
    {
        "code": 0,
        "msg": "success",
        "request_id": "req-1",
        "data": {
            "kind": "ok",
            "quota": {
                "usages": {
                    "limit5h": {"usedRatio": 0.25, "resetAt": "2026-01-01T05:00:00Z"},
                    "limit7d": {"usedRatio": 0.4, "resetAt": "2026-01-05T00:00:00Z"},
                    "monthTotal": {"usedRatio": 0.5, "resetAt": "2026-02-01T00:00:00Z"},
                    "monthCode": {"usedRatio": 0.75, "resetAt": "2026-02-01T00:00:00Z"},
                },
                "extraUsage": None,
            },
        },
    }
).encode()

CONFIG = """\
[[pools]]
id = "kimi-code"
title = "Kimi Code"
unit = "percent"

[[pools.windows]]
id = "5h"
duration = "5h"
kind = "rolling"
role = "guard"

[[pools.windows]]
id = "month"
duration = "1mo"
kind = "fixed"
role = "pace"

[pools.usage]
source = "kimi-code"
windows = { limit5h = "5h", monthTotal = "month" }

[[routes]]
id = "route"
pool = "kimi-code"
backend = "kimi"
model = "m"
tiers = ["standard"]
quality = 3
"""


def found(_name: str) -> ResolvedExecutable:
    return ResolvedExecutable(CLI, False)


def missing(_name: str) -> ResolvedExecutable:
    return ResolvedExecutable(None, False)


def responding(payload: bytes, status: int = 200, seen: list[httpx.Request] | None = None):
    """A MockTransport handler that answers every request the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=payload, headers={"Content-Type": "application/json"})

    return handler


class FakeRun:
    """Stands in for run_server: fails, or hands a fixed origin and token to the callback."""

    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_s: float,
        on_ready: Callable[[str, str, float], UsageRead],
    ) -> tuple[str | None, UsageRead | None]:
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        if self.failure is not None:
            return self.failure, None
        return None, on_ready(ORIGIN, TOKEN, timeout_s)


def build(payload: bytes = OK_BODY, status: int = 200, run=None, resolve=found) -> KimiCodeUsage:
    """A source whose HTTP goes through a mock transport; no process or network is involved."""
    client = httpx.Client(transport=httpx.MockTransport(responding(payload, status)))
    return KimiCodeUsage({}, run=run or FakeRun(), resolve=resolve, client=client)


def test_ok_read_reports_every_quota_field() -> None:
    run = FakeRun()
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(responding(OK_BODY, seen=seen)))
    usage = KimiCodeUsage({}, run=run, resolve=found, client=client)

    read = usage.read()

    assert read.ok
    assert read.points == (
        UsagePoint("limit5h", 0.25, datetime(2026, 1, 1, 5, tzinfo=UTC)),
        UsagePoint("limit7d", 0.4, datetime(2026, 1, 5, tzinfo=UTC)),
        UsagePoint("monthTotal", 0.5, datetime(2026, 2, 1, tzinfo=UTC)),
        UsagePoint("monthCode", 0.75, datetime(2026, 2, 1, tzinfo=UTC)),
    )
    assert run.calls == [[str(CLI), *USAGE_ARGS]]
    assert run.timeouts == [60.0]
    assert str(seen[0].url) == ORIGIN + USAGE_PATH
    assert seen[0].method == "GET"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["Accept"] == "application/json"


def changed_body(**usages: object) -> bytes:
    body = json.loads(OK_BODY)
    body["data"]["quota"]["usages"].update(usages)
    return json.dumps(body).encode()


def test_missing_and_unknown_meters_are_skipped() -> None:
    payload = json.dumps(
        {
            "data": {
                "kind": "ok",
                "quota": {"usages": {"monthCode": {"usedRatio": 0.5, "resetAt": None}}},
            }
        }
    ).encode()

    read = build(payload).read()

    assert read.points == (UsagePoint("monthCode", 0.5, None),)


@pytest.mark.parametrize("ratio", [-0.1, 1.5, "half", True, None, float("nan")])
def test_bad_ratio_skips_only_that_meter(ratio: object) -> None:
    limit5h = {"usedRatio": ratio, "resetAt": None}

    read = build(changed_body(limit5h=limit5h)).read()

    assert read.ok
    assert [point.field for point in read.points] == ["limit7d", "monthTotal", "monthCode"]


@pytest.mark.parametrize("reset", ["soon", True, 123, "2026-02-01T00:00:00"])
def test_bad_reset_skips_only_that_meter(reset: object) -> None:
    limit5h = {"usedRatio": 0.25, "resetAt": reset}  # a naive timestamp has no timezone

    read = build(changed_body(limit5h=limit5h)).read()

    assert read.ok
    assert [point.field for point in read.points] == ["limit7d", "monthTotal", "monthCode"]


@pytest.mark.parametrize("failure", ["timeout", "not_found", "unsupported_version", "error"])
def test_runner_failure_is_the_reason(failure: str) -> None:
    assert build(run=FakeRun(failure=failure)).read().reason == failure


def test_missing_cli_is_not_found() -> None:
    run = FakeRun()

    read = build(run=run, resolve=missing).read()

    assert read.reason == "not_found"
    assert run.calls == []


def test_no_ready_line_is_timeout() -> None:
    run = FakeRun(failure="timeout")

    read = build(run=run).read()

    assert read.reason == "timeout"


def test_data_error_401_is_unauthorized() -> None:
    payload = json.dumps(
        {"code": 0, "data": {"kind": "error", "status": 401, "message": "not signed in"}}
    ).encode()

    assert build(payload).read().reason == "unauthorized"


@pytest.mark.parametrize("status", [404, 500, None])
def test_other_data_errors_are_error(status: int | None) -> None:
    payload = json.dumps({"code": 0, "data": {"kind": "error", "status": status}}).encode()

    assert build(payload).read().reason == "error"


def test_local_http_401_is_unauthorized() -> None:
    assert build(b"{}", status=401).read().reason == "unauthorized"


@pytest.mark.parametrize("status", [403, 500])
def test_other_http_failures_are_error(status: int) -> None:
    assert build(b"{}", status=status).read().reason == "error"


def test_http_timeout_is_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert KimiCodeUsage({}, run=FakeRun(), resolve=found, client=client).read().reason == "timeout"


def test_http_transport_error_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection closed", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert KimiCodeUsage({}, run=FakeRun(), resolve=found, client=client).read().reason == "error"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"",
        b"{}",
        b'{"data": null}',
        b'{"data": {"kind": "ok"}}',
        b'{"data": {"kind": "ok", "quota": {"usages": {}}}}',
    ],
)
def test_malformed_body_reports_reason(payload: bytes) -> None:
    assert build(payload).read().reason == "malformed_response"


def test_malformed_body_logs_only_its_redacted_shape(caplog: pytest.LogCaptureFixture) -> None:
    payload = b'{"data": {"kind": "ok", "note": "secret-note-sentinel-564738"}}'

    with caplog.at_level(logging.WARNING):
        read = build(payload).read()

    assert read.reason == "malformed_response"
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "kimi-code" in message
    assert f"bytes={len(payload)}" in message
    assert json.loads(message.split(" shape=", 1)[1]) == {
        "data": {"kind": {"string_length": 2}, "note": {"string_length": 27}}
    }
    assert "secret-note-sentinel-564738" not in caplog.text


def test_token_never_leaks(caplog: pytest.LogCaptureFixture) -> None:
    cases = [
        build(),  # success
        build(b"not json"),  # malformed body
        build(b"{}", status=401),  # local HTTP 401
        build(run=FakeRun(failure="error")),
    ]

    with caplog.at_level(logging.DEBUG):
        reads = [item.read() for item in cases]

    for read in reads:
        assert TOKEN not in repr(read)
        assert TOKEN not in (read.reason or "")
        assert TOKEN not in (read.warning or "")
    assert TOKEN not in caplog.text


def test_factory_uses_the_configured_executable() -> None:
    usage = PoolUsage(source="kimi-code", executable="D:/bin/kimi.exe", windows={})

    assert SOURCE.factory(usage, {})._executable == "D:/bin/kimi.exe"
    assert SOURCE.factory(PoolUsage(source="kimi-code"), {})._executable == "kimi"


def test_source_is_registered() -> None:
    spec = registry.load_usage_source("kimi-code")

    assert spec is SOURCE
    assert spec.windows == frozenset({"limit5h", "limit7d", "monthTotal", "monthCode"})
    assert spec.observation_source == "kimi_code_usage"
    assert spec.default_api_key_env is None
    assert spec.check is check


def test_config_with_the_source_validates(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")

    config = load_config(path)

    assert config.pools[0].usage.source == "kimi-code"
    assert dict(config.pools[0].usage.windows) == {"limit5h": "5h", "monthTotal": "month"}
    assert registry.validate(config) == []


# --- the process runner ---------------------------------------------------------------------


class FakeProcess:
    """A Popen stand-in: canned stdout lines and stderr, alive until wait or kill."""

    pid = 4242

    def __init__(self, stdout: Sequence[bytes] = (), stderr: bytes = b"", alive: bool = True):
        self.stdout = list(stdout)
        self.stderr = [stderr] if stderr else []
        self.stdin = None
        self._alive = alive
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 1

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        return 1

    def kill(self) -> None:
        self._alive = False
        self.killed = True


class FakePopen:
    """A Popen factory returning a prepared FakeProcess and recording its arguments."""

    def __init__(self, proc: FakeProcess | None = None, error: Exception | None = None) -> None:
        self.proc = proc
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> FakeProcess:
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        assert self.proc is not None
        return self.proc


@pytest.fixture
def killed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    pids: list[int] = []
    monkeypatch.setattr(kimi_code_usage, "kill_tree", lambda pid: pids.append(pid))
    return pids


def fail_on_ready(origin: str, token: str, timeout_s: float) -> UsageRead:
    raise AssertionError("on_ready must not run without a ready line")


def test_ready_line_reaches_on_ready_and_the_tree_is_stopped(killed: list[int]) -> None:
    proc = FakeProcess(stdout=[READY_LINE.encode()])
    popen = FakePopen(proc)
    seen: list[tuple[str, str, float]] = []

    def on_ready(origin: str, token: str, timeout_s: float) -> UsageRead:
        seen.append((origin, token, timeout_s))
        return UsageRead(points=(UsagePoint("limit5h", 0.25),))

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, on_ready, popen=popen)

    assert failure is None
    assert read is not None and read.ok
    assert seen == [(ORIGIN, TOKEN, pytest.approx(60.0, abs=1.0))]
    assert popen.calls == [["kimi", *USAGE_ARGS]]
    assert killed == [4242]  # the server stays up for the HTTP read and is stopped after


def test_no_ready_line_within_the_budget_is_timeout(killed: list[int]) -> None:
    popen = FakePopen(FakeProcess())  # alive and silent

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 0.0, fail_on_ready, popen=popen)

    assert (failure, read) == ("timeout", None)
    assert killed == [4242]


def test_unknown_option_is_unsupported_version(killed: list[int]) -> None:
    proc = FakeProcess(stderr=b"error: unknown option '--json'\n", alive=False)
    popen = FakePopen(proc)

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, fail_on_ready, popen=popen)

    assert (failure, read) == ("unsupported_version", None)
    assert killed == []  # the process ended on its own, nothing is left to stop


def test_process_exit_without_a_ready_line_is_error(killed: list[int]) -> None:
    popen = FakePopen(FakeProcess(alive=False))

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, fail_on_ready, popen=popen)

    assert (failure, read) == ("error", None)
    assert killed == []


def test_start_failure_is_not_found(killed: list[int]) -> None:
    failure, read = run_server(
        ["kimi", *USAGE_ARGS], {}, 60.0, fail_on_ready, popen=FakePopen(error=OSError("no"))
    )

    assert (failure, read) == ("not_found", None)
    assert killed == []


def test_bad_ready_line_is_malformed_and_stops_the_tree(
    killed: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    line = json.dumps({"note": "not a ready line", "token": TOKEN}).encode()
    popen = FakePopen(FakeProcess(stdout=[line]))

    with caplog.at_level(logging.DEBUG):
        failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, fail_on_ready, popen=popen)

    assert (failure, read) == ("malformed_response", None)
    assert killed == [4242]
    # the ready line's stream may carry the token, so nothing of it is logged
    assert TOKEN not in caplog.text
    assert caplog.records == []


def test_text_before_the_ready_line_is_skipped(killed: list[int]) -> None:
    # a notice such as an update hint may precede the ready line
    lines = [b"A new version of Kimi Code is available\n", READY_LINE.encode()]
    popen = FakePopen(FakeProcess(stdout=lines))

    def on_ready(origin: str, token: str, timeout_s: float) -> UsageRead:
        return UsageRead(points=(UsagePoint("limit5h", 0.25),))

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, on_ready, popen=popen)

    assert failure is None
    assert read is not None and read.ok
    assert killed == [4242]


def test_unknown_option_on_stdout_is_unsupported_version(killed: list[int]) -> None:
    popen = FakePopen(FakeProcess(stdout=[b"error: unknown option '--json'\n"], alive=False))

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, fail_on_ready, popen=popen)

    assert (failure, read) == ("unsupported_version", None)


def test_on_ready_exception_is_error_and_stops_the_tree(killed: list[int]) -> None:
    popen = FakePopen(FakeProcess(stdout=[READY_LINE.encode()]))

    def broken(origin: str, token: str, timeout_s: float) -> UsageRead:
        raise RuntimeError("boom")

    failure, read = run_server(["kimi", *USAGE_ARGS], {}, 60.0, broken, popen=popen)

    assert (failure, read) == ("error", None)
    assert killed == [4242]


def test_usage_read_ends_when_the_runner_reports_a_failure(killed: list[int]) -> None:
    popen = FakePopen(FakeProcess(stdout=[READY_LINE.encode()]))

    usage = KimiCodeUsage(
        {},
        resolve=found,
        run=lambda argv, env, timeout_s, on_ready: run_server(
            argv, env, timeout_s, on_ready, popen=popen
        ),
        client=httpx.Client(transport=httpx.MockTransport(responding(OK_BODY))),
    )

    read = usage.read()

    assert read.ok
    assert killed == [4242]


# --- doctor check ---------------------------------------------------------------------------


class FakeCommand:
    def __init__(self, output: str | None) -> None:
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> str | None:
        self.calls.append(list(argv))
        return self.output


def test_check_is_quiet_on_a_new_enough_cli() -> None:
    command = FakeCommand("Kimi Code 2.8.1\n")

    assert check(PoolUsage(source="kimi-code"), {}, command=command, resolve=found) is None
    assert command.calls == [[str(CLI), "--version"]]  # the server is never started


@pytest.mark.parametrize("output", ["Kimi Code 2.8.0\n", "kimi 2.7.3"])
def test_check_reports_a_too_old_cli(output: str) -> None:
    warning = check(PoolUsage(source="kimi-code"), {}, command=FakeCommand(output), resolve=found)

    assert warning == (
        f"Kimi Code {MIN_VERSION} or newer is needed for usage reads; run kimi upgrade"
    )


@pytest.mark.parametrize("output", [None, "no version here"])
def test_check_is_quiet_when_the_version_is_unknown(output: str | None) -> None:
    usage = PoolUsage(source="kimi-code")

    assert check(usage, {}, command=FakeCommand(output), resolve=found) is None


def test_check_reports_a_missing_cli() -> None:
    usage = PoolUsage(source="kimi-code", executable="D:/bin/kimi.exe")

    warning = check(usage, {}, command=FakeCommand("Kimi Code 2.8.1\n"), resolve=missing)

    assert warning == (
        "cannot find the Kimi Code CLI 'D:/bin/kimi.exe'; install it or set executable"
    )
