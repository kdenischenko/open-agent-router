"""Claude Code usage source: stream parsing, reason codes, process handling and doctor check."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_router import registry
from agent_router.collectors import claude_code_usage
from agent_router.collectors.claude_code_usage import (
    SOURCE,
    STATUS_ARGS,
    USAGE_ARGS,
    ClaudeCodeUsage,
    check,
    run_command,
    run_stream,
)
from agent_router.config_model import Pool, PoolUsage, Window
from agent_router.periods import Duration
from agent_router.runner import ResolvedExecutable
from agent_router.usage import UsagePoint, observations

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_usage"
CLI = Path("C:/tools/claude.exe") if sys.platform == "win32" else Path("/opt/claude/bin/claude")
NOW = datetime(2026, 11, 22, 19, tzinfo=UTC)
SIGNED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
SIGNED_OUT = json.dumps({"loggedIn": False, "authMethod": "none"})


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
        for line in self.lines:
            self.fed += 1
            if not on_line(line):
                self.stopped = True
                return None
        return self.failure


class FakeCommand:
    def __init__(self, stdout: str | None) -> None:
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> str | None:
        self.calls.append(list(argv))
        return self.stdout


def build(stream: FakeStream, command: FakeCommand | None = None, resolve=found) -> ClaudeCodeUsage:
    return ClaudeCodeUsage(
        {}, run=stream, command=command or FakeCommand(SIGNED_IN), resolve=resolve
    )


def claude_pool() -> Pool:
    five = Window("5h", Duration(hours=5), "rolling", "guard", None, None, 1.0)
    week = Window("week", Duration(days=7), "fixed", "pace", None, None, 1.0)
    usage = PoolUsage(source="claude-code", windows={"session": "5h", "weekly_all": "week"})
    return Pool(
        "claude-pro",
        "Claude Pro",
        "percent",
        (five, week),
        0.0,
        None,
        None,
        None,
        None,
        usage=usage,
    )


def test_signed_in_read_reports_session_and_week() -> None:
    stream = FakeStream(fixture_lines("signed_in.jsonl"))
    read = build(stream).read()

    assert read.ok
    assert read.points == (
        UsagePoint("session", 0.12, datetime(2026, 11, 22, 23, 10, 0, 258546, tzinfo=UTC)),
        UsagePoint("weekly_all", 0.37, datetime(2026, 11, 28, 6, 0, 0, 258566, tzinfo=UTC)),
    )
    assert stream.calls == [[str(CLI), *USAGE_ARGS]]
    assert read.warning is None


@pytest.mark.parametrize(
    ("failure", "warning"),
    [
        ("timeout", "the Claude Code CLI timed out after returning a usage report"),
        ("not_found", "the Claude Code CLI was not found while reading usage"),
        ("exit_1", "the Claude Code CLI failed after returning a usage report"),
    ],
)
def test_valid_report_survives_process_failure(failure: str, warning: str) -> None:
    read = build(FakeStream(fixture_lines("signed_in.jsonl"), failure=failure)).read()

    assert read.ok
    assert [point.field for point in read.points] == ["session", "weekly_all"]
    assert read.warning == warning


def test_command_runs_print_mode_without_user_settings() -> None:
    assert USAGE_ARGS[:2] == ("-p", "/usage")
    assert "--setting-sources" in USAGE_ARGS
    assert USAGE_ARGS[USAGE_ARGS.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--verbose", "--no-session-persistence"):
        assert flag in USAGE_ARGS
    assert USAGE_ARGS[USAGE_ARGS.index("--output-format") + 1] == "stream-json"


def test_observations_carry_the_source_name() -> None:
    read = build(FakeStream(fixture_lines("signed_in.jsonl"))).read()

    result = observations(claude_pool(), SOURCE, read, NOW)

    assert [(item.window, item.fraction, item.source) for item in result] == [
        ("5h", 0.12, "claude_code_usage"),
        ("week", 0.37, "claude_code_usage"),
    ]
    assert result[0].resets_at is None  # a rolling window has no reset of its own
    assert result[1].resets_at == datetime(2026, 11, 28, 6, 0, 0, 258566, tzinfo=UTC)


def test_signed_out_cli_is_unauthorized() -> None:
    command = FakeCommand(SIGNED_OUT)
    read = build(FakeStream(fixture_lines("signed_out.jsonl")), command).read()

    assert read.reason == "unauthorized"
    assert command.calls == [[str(CLI), *STATUS_ARGS]]


def test_rate_limits_unavailable_is_unauthorized() -> None:
    lines = fixture_lines("signed_in.jsonl")
    event = json.loads(lines[1])
    event["usage_report"]["rate_limits_available"] = False
    event["usage_report"]["rate_limits"] = None
    lines[1] = json.dumps(event) + "\n"

    assert build(FakeStream(lines)).read().reason == "unauthorized"


def test_rate_limited_usage_endpoint_is_reported() -> None:
    read = build(FakeStream(fixture_lines("rate_limited.jsonl"))).read()

    assert read.reason == "rate_limited"


def test_missing_usage_report_is_malformed() -> None:
    command = FakeCommand(SIGNED_IN)
    read = build(FakeStream(fixture_lines("no_usage_report.jsonl")), command).read()

    assert read.reason == "malformed_response"
    assert command.calls == [[str(CLI), *STATUS_ARGS]]


def test_malformed_stream_logs_event_types_without_answer_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_text = "cli-answer-text-sentinel-846291"
    secret_id = "cli-answer-id-sentinel-375864"
    secret_number = 726354819
    raw = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "type": "message",
                    "model": "<synthetic>",
                    "id": secret_id,
                    "content": [{"type": "text", "text": secret_text}],
                },
                "local_command_run": {"command": "usage"},
                "attempt": secret_number,
            }
        )
        + "\n"
    )

    with caplog.at_level(logging.WARNING):
        read = build(FakeStream([raw]), FakeCommand(SIGNED_IN)).read()

    assert read.reason == "malformed_response"
    message = caplog.records[-1].getMessage()
    assert caplog.records[-1].levelno == logging.WARNING
    assert "claude-code" in message
    assert f"bytes={len(raw.encode())}" in message
    assert json.loads(message.split(" shape=", 1)[1]) == {
        "type": "assistant",
        "message": {
            "type": "message",
            "model": {"string_length": 11},
            "id": {"string_length": len(secret_id)},
            "content": {
                "list_length": 1,
                "items": [{"type": "text", "text": {"string_length": len(secret_text)}}],
            },
        },
        "local_command_run": {"command": {"string_length": 5}},
        "attempt": "number",
    }
    for secret in (secret_text, secret_id, str(secret_number), "<synthetic>"):
        assert secret not in caplog.text


def test_unusable_sign_in_status_is_malformed() -> None:
    read = build(FakeStream(fixture_lines("signed_out.jsonl")), FakeCommand(None)).read()

    assert read.reason == "malformed_response"


def test_model_call_stops_the_process_and_is_malformed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = fixture_lines("model_called.jsonl") + fixture_lines("signed_in.jsonl")[1:]
    stream = FakeStream(lines)

    with caplog.at_level(logging.WARNING):
        read = build(stream).read()

    assert read.reason == "malformed_response"
    assert stream.stopped
    assert stream.fed == 2  # the init line, then the model's message
    assert caplog.records[-1].levelno == logging.WARNING
    assert '"type": "assistant"' in caplog.records[-1].getMessage()
    assert "I can't open that path." not in caplog.text


def test_model_call_after_the_usage_message_still_fails() -> None:
    lines = fixture_lines("signed_in.jsonl")[:2] + fixture_lines("model_called.jsonl")[1:]

    assert build(FakeStream(lines)).read().reason == "malformed_response"


def test_missing_cli_is_not_found() -> None:
    stream = FakeStream(fixture_lines("signed_in.jsonl"))

    read = build(stream, resolve=missing).read()

    assert read.reason == "not_found"
    assert stream.calls == []


@pytest.mark.parametrize("failure", ["timeout", "not_found"])
def test_process_failures_report_reason_codes(failure: str) -> None:
    read = build(FakeStream([], failure=failure)).read()

    assert read.reason == failure


@pytest.mark.parametrize(
    "row",
    [
        {"kind": "weekly_scoped", "percent": 10, "resets_at": None},
        {"kind": "unknown", "percent": 10, "resets_at": None},
        {"kind": "session", "percent": 150, "resets_at": None},
        {"kind": "session", "percent": -1, "resets_at": None},
        {"kind": "session", "percent": "lots", "resets_at": None},
        {"kind": "session", "percent": True, "resets_at": None},
        {"kind": "session", "percent": 10, "resets_at": "soon"},
        {"kind": "session", "percent": 10, "resets_at": 1795389000},
        "broken",
    ],
)
def test_bad_row_is_skipped_others_kept(row: object) -> None:
    lines = fixture_lines("signed_in.jsonl")
    event = json.loads(lines[1])
    limits = event["usage_report"]["rate_limits"]["limits"]
    limits[0] = row
    lines[1] = json.dumps(event) + "\n"

    read = build(FakeStream(lines)).read()

    assert read.ok
    assert [point.field for point in read.points] == ["weekly_all"]


def test_no_usable_row_is_malformed() -> None:
    lines = fixture_lines("signed_in.jsonl")
    event = json.loads(lines[1])
    event["usage_report"]["rate_limits"]["limits"] = [{"kind": "session", "percent": 500}]
    lines[1] = json.dumps(event) + "\n"

    assert build(FakeStream(lines)).read().reason == "malformed_response"


def test_non_json_lines_are_ignored() -> None:
    lines = ["warming up\n", *fixture_lines("signed_in.jsonl")]

    assert build(FakeStream(lines)).read().ok


def test_factory_uses_the_configured_executable() -> None:
    usage = PoolUsage(source="claude-code", executable="D:/bin/claude.exe", windows={})
    source = SOURCE.factory(usage, {})

    assert source._executable == "D:/bin/claude.exe"
    assert SOURCE.factory(PoolUsage(source="claude-code"), {})._executable == "claude"


def test_source_is_registered() -> None:
    spec = registry.load_usage_source("claude-code")

    assert spec is SOURCE
    assert spec.windows == frozenset({"session", "weekly_all"})
    assert spec.default_api_key_env is None


# --- doctor check ------------------------------------------------------------------------


def test_check_is_quiet_when_signed_in() -> None:
    command = FakeCommand(SIGNED_IN)

    assert check(PoolUsage(source="claude-code"), {}, command=command, resolve=found) is None
    assert command.calls == [[str(CLI), *STATUS_ARGS]]


def test_check_reports_a_missing_cli() -> None:
    usage = PoolUsage(source="claude-code", executable="D:/bin/claude.exe")

    warning = check(usage, {}, command=FakeCommand(SIGNED_IN), resolve=missing)

    assert (
        warning
        == "cannot find the Claude Code CLI 'D:/bin/claude.exe'; install it or set executable"
    )


def test_check_reports_a_cli_that_is_not_signed_in() -> None:
    warning = check(
        PoolUsage(source="claude-code"), {}, command=FakeCommand(SIGNED_OUT), resolve=found
    )

    assert warning == "the Claude Code CLI is not signed in; run claude auth login"


@pytest.mark.parametrize("stdout", [None, "not json", "{}", '{"loggedIn": "yes"}'])
def test_check_reports_an_unusable_status(stdout: str | None) -> None:
    warning = check(PoolUsage(source="claude-code"), {}, command=FakeCommand(stdout), resolve=found)

    assert warning == "the Claude Code CLI did not report its sign-in status"


# --- process handling: a Python stub stands in for the CLI --------------------------------


def _python(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def test_run_stream_feeds_every_line() -> None:
    seen: list[str] = []
    code = "print('{\"a\": 1}'); print('{\"b\": 2}')"

    failure = run_stream(
        _python(code), dict(os.environ), 30, lambda line: seen.append(line) or True
    )

    assert failure is None
    assert [line.strip() for line in seen] == ['{"a": 1}', '{"b": 2}']


def test_run_stream_stops_the_process_on_request() -> None:
    seen: list[str] = []
    code = "import time\nprint('first', flush=True)\ntime.sleep(60)\nprint('second')"

    failure = run_stream(
        _python(code), dict(os.environ), 30, lambda line: seen.append(line) and False
    )

    assert failure is None
    assert [line.strip() for line in seen] == ["first"]


def test_run_stream_times_out() -> None:
    code = "import time\ntime.sleep(60)"

    assert run_stream(_python(code), dict(os.environ), 0.5, lambda line: True) == "timeout"


def test_run_stream_reports_a_missing_program(tmp_path: Path) -> None:
    program = str(tmp_path / "no-such-cli.exe")

    assert run_stream([program, "-p"], dict(os.environ), 5, lambda line: True) == "not_found"


def test_run_command_returns_stdout_whatever_the_exit_code() -> None:
    code = "import sys\nprint('{\"loggedIn\": false}')\nsys.exit(1)"

    assert json.loads(run_command(_python(code), dict(os.environ), 30) or "") == {"loggedIn": False}


def test_run_command_times_out() -> None:
    assert run_command(_python("import time\ntime.sleep(60)"), dict(os.environ), 0.5) is None


class _RunningProcess:
    pid = 4242

    def __init__(self) -> None:
        self.stdout = iter([b"line\n"])
        self.returncode: int | None = None
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_timeouts.append(timeout)
        return self.returncode

    def communicate(self, timeout: float) -> None:
        raise RuntimeError("command failed")


def test_run_stream_callback_error_kills_and_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _RunningProcess()
    monkeypatch.setattr(claude_code_usage.subprocess, "Popen", lambda *args, **kwargs: proc)

    def fail(_line: str) -> bool:
        raise RuntimeError("line failed")

    with pytest.raises(RuntimeError, match="line failed"):
        run_stream(["claude"], {}, 30, fail)

    assert proc.killed is True
    assert proc.wait_timeouts == [None]


def test_run_command_unexpected_error_kills_and_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _RunningProcess()
    monkeypatch.setattr(claude_code_usage.subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(RuntimeError, match="command failed"):
        run_command(["claude"], {}, 30)

    assert proc.killed is True
    assert proc.wait_timeouts == [None]


def test_module_never_reads_claude_credentials() -> None:
    text = Path(claude_code_usage.__file__).read_text(encoding="utf-8")

    assert ".credentials" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text
