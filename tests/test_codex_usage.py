"""Codex usage source: JSON-RPC stream parsing, reason codes and doctor check."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_router import __version__, registry
from agent_router.collectors import codex_usage
from agent_router.collectors.codex_usage import (
    REQUESTS,
    SOURCE,
    STATUS_ARGS,
    USAGE_ARGS,
    CodexUsage,
    check,
)
from agent_router.config_model import PoolUsage
from agent_router.runner import ResolvedExecutable
from agent_router.usage import UsagePoint

FIXTURES = Path(__file__).parent / "fixtures" / "codex_usage"
CLI = Path("C:/tools/codex.cmd") if sys.platform == "win32" else Path("/opt/codex/bin/codex")
SIGNED_IN = "Logged in using ChatGPT\n"
SIGNED_OUT = "Not logged in\n"


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)


def found(_name: str) -> ResolvedExecutable:
    return ResolvedExecutable(CLI, False)


def missing(_name: str) -> ResolvedExecutable:
    return ResolvedExecutable(None, False)


class FakeStream:
    """Feeds recorded lines to the parser the way run_stream would, recording the calls."""

    def __init__(self, lines: Sequence[str], failure: str | None = None) -> None:
        self.lines = lines
        self.failure = failure
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.fed = 0
        self.stopped = False

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_s: float,
        on_line: Callable[[str], bool],
    ) -> str | None:
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        for line in self.lines:
            self.fed += 1
            if not on_line(line):
                self.stopped = True
                return None
        return self.failure


class FakeCommand:
    def __init__(self, output: str | None) -> None:
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> str | None:
        self.calls.append(list(argv))
        return self.output


def build(stream: FakeStream, command: FakeCommand | None = None, resolve=found) -> CodexUsage:
    return CodexUsage({}, run=stream, command=command or FakeCommand(SIGNED_IN), resolve=resolve)


def changed_limits(**changes: object) -> list[str]:
    lines = fixture_lines("signed_in.jsonl")
    response = json.loads(lines[2])
    response["result"]["rateLimits"].update(changes)
    lines[2] = json.dumps(response) + "\n"
    return lines


def test_signed_in_read_reports_five_hour_and_week() -> None:
    stream = FakeStream(fixture_lines("signed_in.jsonl"))

    read = build(stream).read()

    assert read.ok
    assert read.points == (
        UsagePoint("primary", 0.08, datetime(2026, 11, 28, 14, 24, 13, tzinfo=UTC)),
        UsagePoint("secondary", 0.01, datetime(2026, 12, 5, 9, 24, 13, tzinfo=UTC)),
    )
    assert stream.calls == [[str(CLI), *USAGE_ARGS]]
    assert stream.timeouts == [60.0]
    assert stream.stopped
    assert stream.fed == 3  # the final account/updated notification is not read


def test_json_rpc_requests_initialize_and_read_rate_limits() -> None:
    messages = [json.loads(line) for line in REQUESTS]

    assert messages == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "agent-router", "version": __version__}},
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
    ]
    assert USAGE_ARGS == ("app-server",)


def test_signed_out_error_is_unauthorized() -> None:
    stream = FakeStream(fixture_lines("signed_out.jsonl"))

    read = build(stream).read()

    assert read.reason == "unauthorized"
    assert stream.stopped


def test_non_auth_json_rpc_error_is_error() -> None:
    lines = fixture_lines("signed_out.jsonl")
    response = json.loads(lines[1])
    response["error"]["message"] = "internal server failure"
    lines[1] = json.dumps(response) + "\n"

    assert build(FakeStream(lines)).read().reason == "error"


def test_missing_rate_limits_while_signed_out_is_unauthorized() -> None:
    lines = changed_limits()
    response = json.loads(lines[2])
    del response["result"]["rateLimits"]
    lines[2] = json.dumps(response) + "\n"
    command = FakeCommand(SIGNED_OUT)

    read = build(FakeStream(lines), command).read()

    assert read.reason == "unauthorized"
    assert command.calls == [[str(CLI), *STATUS_ARGS]]


def test_missing_rate_limits_with_unusable_status_is_malformed() -> None:
    lines = fixture_lines("signed_in.jsonl")
    response = json.loads(lines[2])
    del response["result"]["rateLimits"]
    lines[2] = json.dumps(response) + "\n"

    read = build(FakeStream(lines), FakeCommand(None)).read()

    assert read.reason == "malformed_response"


def test_null_primary_keeps_secondary() -> None:
    read = build(FakeStream(changed_limits(primary=None))).read()

    assert read.ok
    assert [point.field for point in read.points] == ["secondary"]


@pytest.mark.parametrize("percent", [-1, 101, "8", True, None, float("nan")])
def test_bad_percent_skips_only_that_window(percent: object) -> None:
    primary = {"usedPercent": percent, "windowDurationMins": 300, "resetsAt": None}

    read = build(FakeStream(changed_limits(primary=primary))).read()

    assert read.ok
    assert [point.field for point in read.points] == ["secondary"]


@pytest.mark.parametrize("reset", ["soon", True, float("inf")])
def test_bad_reset_skips_only_that_window(reset: object) -> None:
    primary = {"usedPercent": 8.5, "windowDurationMins": 300, "resetsAt": reset}

    read = build(FakeStream(changed_limits(primary=primary))).read()

    assert read.ok
    assert [point.field for point in read.points] == ["secondary"]


def test_null_reset_is_accepted() -> None:
    primary = {"usedPercent": 8.5, "windowDurationMins": 300, "resetsAt": None}

    read = build(FakeStream(changed_limits(primary=primary, secondary=None))).read()

    assert read.ok
    assert read.points == (UsagePoint("primary", 0.085, None),)


def test_no_valid_points_is_malformed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    primary = {"usedPercent": 500, "windowDurationMins": 300, "resetsAt": None}
    lines = changed_limits(primary=primary, secondary=None)

    with caplog.at_level(logging.WARNING):
        read = build(FakeStream(lines)).read()

    assert read.reason == "malformed_response"
    assert "codex" in caplog.records[-1].getMessage()
    assert "500" not in caplog.text


def test_timeout_without_response_is_timeout() -> None:
    assert build(FakeStream([], failure="timeout")).read().reason == "timeout"


def test_process_failure_without_response_is_not_found() -> None:
    assert build(FakeStream([], failure="not_found")).read().reason == "not_found"


def test_missing_cli_is_not_found() -> None:
    stream = FakeStream(fixture_lines("signed_in.jsonl"))

    read = build(stream, resolve=missing).read()

    assert read.reason == "not_found"
    assert stream.calls == []


def test_notifications_and_non_json_lines_are_ignored() -> None:
    lines = [
        "warming up\n",
        '{"jsonrpc":"2.0","method":"account/updated","params":{"authMode":"chatgpt"}}\n',
        *fixture_lines("signed_in.jsonl"),
    ]

    assert build(FakeStream(lines)).read().ok


def test_factory_uses_the_configured_executable() -> None:
    usage = PoolUsage(source="codex", executable="D:/bin/codex.cmd", windows={})

    assert SOURCE.factory(usage, {})._executable == "D:/bin/codex.cmd"
    assert SOURCE.factory(PoolUsage(source="codex"), {})._executable == "codex"


def test_source_is_registered() -> None:
    spec = registry.load_usage_source("codex")

    assert spec is SOURCE
    assert spec.windows == frozenset({"primary", "secondary"})
    assert spec.observation_source == "codex_usage"
    assert spec.default_api_key_env is None


def test_module_never_reads_codex_credentials() -> None:
    text = Path(codex_usage.__file__).read_text(encoding="utf-8")

    assert "auth.json" not in text
    assert "OPENAI_API_KEY" not in text


# --- doctor check ------------------------------------------------------------------------


def test_check_is_quiet_when_signed_in() -> None:
    command = FakeCommand(SIGNED_IN)

    assert check(PoolUsage(source="codex"), {}, command=command, resolve=found) is None
    assert command.calls == [[str(CLI), *STATUS_ARGS]]


def test_check_reports_a_missing_cli() -> None:
    usage = PoolUsage(source="codex", executable="D:/bin/codex.cmd")

    warning = check(usage, {}, command=FakeCommand(SIGNED_IN), resolve=missing)

    assert warning == "cannot find the Codex CLI 'D:/bin/codex.cmd'; install it or set executable"


@pytest.mark.parametrize("output", [SIGNED_OUT, None, "unrecognized status"])
def test_check_reports_a_cli_that_is_not_signed_in(output: str | None) -> None:
    warning = check(PoolUsage(source="codex"), {}, command=FakeCommand(output), resolve=found)

    assert warning == "the Codex CLI is not signed in; run codex login"


# answers a request only after a short delay and quits at once on end of input, like the
# app-server, which drops pending requests when its stdin closes
_EOF_QUITTING_SERVER = r"""
import json, os, sys, threading, time
def answer(request_id):
    time.sleep(0.3)
    body = {"id": request_id, "result": {"rateLimits": {
        "primary": {"usedPercent": 8, "windowDurationMins": 300, "resetsAt": None},
        "secondary": None}}}
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 2:
        threading.Thread(target=answer, args=(2,), daemon=True).start()
os._exit(0)
"""


def test_stdin_stays_open_until_the_answer_arrives(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text(_EOF_QUITTING_SERVER, encoding="utf-8")
    stream_lines: list[str] = []

    def feed(line: str) -> bool:
        stream_lines.append(line)
        return json.loads(line).get("id") != 2

    failure = codex_usage.run_stream([sys.executable, str(script)], dict(os.environ), 10.0, feed)

    assert failure is None
    assert any(json.loads(line).get("id") == 2 for line in stream_lines)
