"""Job client commands through a stubbed GatewayClient, without a live server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from agent_router import cli
from agent_router.client import GatewayError, GatewayUnavailable
from agent_router.clock import format_local

_RUN_PROMPT = "сделай задачу"  # non-ascii on purpose
_STDIN_PROMPT = "текст из stdin\n"  # non-ascii on purpose
_BOM_PAYLOAD = "строка 1\r\nстрока 2\r\n"  # non-ascii on purpose
_STRIPPED_PROMPT = "строка 1\nстрока 2\n\rа"  # non-ascii on purpose


def make_job(status: str = "completed", **changes) -> dict:
    job = {
        "id": "job-1",
        "status": status,
        "mode": "read",
        "tier": "standard",
        "tier_source": "default",
        "kind": None,
        "caller": None,
        "route": "fake/main",
        "reason": "test reason",
        "approval": None,
        "attempts": [],
        "text": "done",
        "changed_files": [],
        "violations": [],
        "error": None,
        "log_tail": None,
        "created_at": "2026-11-13T09:00:00+00:00",
        "finished_at": "2026-11-13T09:00:01+00:00" if status != "running" else None,
    }
    job.update(changes)
    return job


class FakeGateway:
    def __init__(self, client_token: str, admin_token: str) -> None:
        self.client_token = client_token
        self.admin_token = admin_token
        self.tokens_used: list[str] = []
        self.job = make_job()
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, object]] = []
        self.unblocked: dict = {"blocked": False, "blocked_by": None, "blocked_until": None}

    def get(self, path: str, **params) -> dict:
        self.calls.append(("GET", path, params))
        if self.error is not None:
            raise self.error
        if path == "/api/v1/jobs":
            return {"jobs": [self.job]}
        return self.job

    def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", path, body))
        if self.error is not None:
            raise self.error
        if path == "/api/v1/jobs":
            return {"job_id": self.job["id"], "status": "queued"}
        if path.endswith("/unblock"):
            return {"pool": path.rsplit("/", 2)[-2], **self.unblocked}
        return self.job


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, config, tokens) -> FakeGateway:
    fake = FakeGateway(tokens.client, tokens.admin)

    def factory(base_url: str, token: str) -> FakeGateway:
        fake.tokens_used.append(token)
        return fake

    monkeypatch.setattr(cli, "make_client", factory)
    return fake


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("completed", 0), ("partial", 2), ("failed", 3), ("declined", 3), ("cancelled", 3)],
)
def test_result_maps_terminal_status_to_exit_code(
    runner: CliRunner, gateway: FakeGateway, status: str, exit_code: int
) -> None:
    gateway.job = make_job(status)

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == exit_code
    assert gateway.calls == [("GET", "/api/v1/jobs/job-1", {})]


def test_run_keeps_service_line_out_of_stdout(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", _RUN_PROMPT])

    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert "[agent-router]" not in result.stdout
    assert result.stderr == (
        "[agent-router] job job-1, status completed, route fake/main, "
        "route choice test reason, files changed: 0\n"
        "rate: agent-router rate job-1 good|bad|partial\n"
    )


@pytest.mark.parametrize(("status", "exit_code"), [("failed", 3), ("partial", 2)])
def test_summary_prints_error_for_failed(
    runner: CliRunner, gateway: FakeGateway, status: str, exit_code: int
) -> None:
    gateway.job = make_job(
        status, text=None, error={"code": "executor_error", "message": "empty output"}
    )

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == exit_code
    assert "error executor_error: empty output" in result.stderr


def _attempt(n: int) -> dict:
    return {"n": n, "route": "fake/main", "pool": "pool-a", "outcome": "error"}


@pytest.mark.parametrize(("status", "exit_code"), [("failed", 3), ("partial", 2)])
def test_summary_names_violations_and_the_log_directory(
    runner: CliRunner, gateway: FakeGateway, home: Path, status: str, exit_code: int
) -> None:
    gateway.job = make_job(
        status,
        text=None,
        attempts=[_attempt(1), _attempt(2)],
        violations=["head_moved", "orphan_process"],
        error={"code": "executor_error", "message": "exit code 1"},
    )

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == exit_code
    assert "violations: head_moved, orphan_process" in result.stderr
    assert f"logs: {home / 'jobs' / 'job-1' / 'attempt-2'}" in result.stderr


def test_summary_shows_the_last_log_lines_of_a_failed_job(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    tail = "\n".join(f"line {n}" for n in range(1, 9)) + "\n"
    gateway.job = make_job(
        "failed",
        text=None,
        attempts=[_attempt(1)],
        log_tail=tail,
        error={"code": "executor_error", "message": "exit code 1"},
    )

    result = runner.invoke(cli.app, ["result", "job-1"])

    lines = result.stderr.splitlines()
    start = lines.index("last log lines:")
    assert lines[start + 1 : start + 6] == [f"  line {n}" for n in range(4, 9)]
    assert lines[start + 6].startswith("rate:")


def test_summary_warns_when_a_read_job_changed_files(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    changed = [f"src/file{n}.py" for n in range(7)]
    gateway.job = make_job("partial", mode="read", changed_files=changed)

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 2
    expected = (
        "warning: the read job changed 7 files: src/file0.py, src/file1.py, src/file2.py, "
        "src/file3.py, src/file4.py and 2 more"
    )
    assert expected in result.stderr


def test_summary_of_a_clean_job_has_no_extra_lines(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("completed", mode="write", changed_files=["src/demo.py"])

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 0
    assert "warning" not in result.stderr
    assert "violations" not in result.stderr
    assert "logs:" not in result.stderr


def test_summary_explains_declined(runner: CliRunner, gateway: FakeGateway) -> None:
    # the gateway sends no error for a declined job
    gateway.job = make_job("declined", text=None, error=None)

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 3
    assert "declined: confirmation refused and no other route matches" in result.stderr


def test_summary_labels_reason_as_route_choice(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 0
    assert "route choice test reason" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["run", _RUN_PROMPT, "--json"],
        ["wait", "job-1", "--json"],
        ["result", "job-1", "--json"],
    ],
)
def test_json_output_is_parseable(runner: CliRunner, gateway: FakeGateway, args: list[str]) -> None:
    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == gateway.job


def test_run_reads_prompt_from_file(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path
) -> None:
    file_prompt = "текст из файла\nвторая строка"  # non-ascii on purpose
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(file_prompt, encoding="utf-8")

    result = runner.invoke(cli.app, ["run", "--no-wait", "--prompt-file", str(prompt_file)])

    assert result.exit_code == 0
    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs")
    assert body["prompt"] == file_prompt


def test_run_reads_prompt_from_stdin(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", "--no-wait", "-"], input=_STDIN_PROMPT)

    assert result.exit_code == 0
    assert gateway.calls[0][2]["prompt"] == _STDIN_PROMPT


def test_run_rejects_non_utf8_stdin(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", "--no-wait", "-"], input=b"\xff")

    assert result.exit_code == 1
    assert "prompt on stdin is not UTF-8" in result.stderr
    assert gateway.calls == []


def test_run_reads_utf8_stdin_bytes(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", "--no-wait", "-"], input=_STDIN_PROMPT.encode())

    assert result.exit_code == 0
    assert gateway.calls[0][2]["prompt"] == _STDIN_PROMPT


def test_run_prompt_file_error_hides_path(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path
) -> None:
    missing = tmp_path / "отсутствующий промпт.txt"  # non-ascii on purpose

    result = runner.invoke(cli.app, ["run", "--prompt-file", str(missing)])

    assert result.exit_code == 1
    assert "could not read prompt file:" in result.stderr
    assert missing.name not in result.stderr
    assert gateway.calls == []


def test_run_prompt_file_not_utf8_is_short_error(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_bytes(b"\xff\xfe\x00broken")

    result = runner.invoke(cli.app, ["run", "--prompt-file", str(prompt_file)])

    assert result.exit_code == 1
    assert "prompt file is not UTF-8" in result.stderr
    assert not isinstance(result.exception, UnicodeDecodeError)
    assert gateway.calls == []


_BOM_CRLF = b"\xef\xbb\xbf" + _BOM_PAYLOAD.encode() + b"\r\xd0\xb0"


def test_run_stdin_drops_bom_and_crlf(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", "--no-wait", "-"], input=_BOM_CRLF)

    assert result.exit_code == 0, result.output
    assert gateway.calls[0][2]["prompt"] == _STRIPPED_PROMPT


def test_run_prompt_file_drops_bom_and_crlf(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_bytes(_BOM_CRLF)

    result = runner.invoke(cli.app, ["run", "--no-wait", "--prompt-file", str(prompt_file)])

    assert result.exit_code == 0, result.output
    assert gateway.calls[0][2]["prompt"] == _STRIPPED_PROMPT


def test_prompt_file_content_is_not_parsed_as_options(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("--mode write", encoding="utf-8")

    result = runner.invoke(cli.app, ["run", "--no-wait", "--prompt-file", str(prompt_file)])

    assert result.exit_code == 0
    assert gateway.calls[0][2]["prompt"] == "--mode write"
    assert gateway.calls[0][2]["mode"] == "read"


def test_no_wait_prints_only_job_id(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["run", "--no-wait", _RUN_PROMPT])

    assert result.exit_code == 0
    assert result.stdout == "job-1\n"
    assert result.stderr == ""
    assert len(gateway.calls) == 1


def test_wait_for_running_job_returns_four_and_hint(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    gateway.job = make_job("running", text=None)

    result = runner.invoke(cli.app, ["wait", "job-1", "--timeout", "1"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert "agent-router wait job-1" in result.stderr
    assert gateway.calls == [("GET", "/api/v1/jobs/job-1/wait", {"timeout": 1.0})]


class FakeClock:
    """Test clock: time advances only by the amount of each requested sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedGateway:
    """Client with scripted responses; a request may advance the clock."""

    def __init__(
        self,
        responses: list[dict] | None,
        clock: FakeClock,
        advance: float = 0.0,
        unavailable: int = 0,
    ) -> None:
        self.responses = responses
        self.clock = clock
        self.advance = advance
        self.unavailable = unavailable
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, **params) -> dict:
        self.calls.append(("GET", path, params))
        self.clock.now += self.advance
        if self.unavailable:
            self.unavailable -= 1
            raise GatewayUnavailable("gateway unavailable")
        if self.responses is None:
            return make_job("running", text=None)
        return self.responses.pop(0)


def _emit_exit_code(job: dict) -> int:
    try:
        cli._emit_job(job, as_json=False)
    except typer.Exit as exc:
        return exc.exit_code
    return 0


def test_wait_long_timeout_joins_short_waits() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway(
        [
            make_job("running", text=None),
            make_job("running", text=None),
            make_job("completed"),
        ],
        clock,
        advance=100.0,
    )

    job = cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert job["status"] == "completed"
    assert [call[2]["timeout"] for call in gateway.calls] == [100.0, 100.0, 50.0]
    assert _emit_exit_code(job) == 0


def test_wait_survives_short_outage() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway([make_job("completed")], clock, unavailable=2)

    job = cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert job["status"] == "completed"
    assert clock.sleeps == [1.0, 1.0]
    assert _emit_exit_code(job) == 0


def test_wait_gives_up_after_10s_outage() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway([], clock, unavailable=11)

    with pytest.raises(GatewayUnavailable):
        cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert clock.sleeps == [1.0] * 10
    assert len(gateway.calls) == 11


def test_wait_outage_measured_in_seconds_not_attempts() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway([], clock, advance=4.0, unavailable=100)

    with pytest.raises(GatewayUnavailable):
        cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert len(gateway.calls) == 3
    assert clock.now <= 16.0


def test_wait_timeout_returns_running(capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    gateway = ScriptedGateway(None, clock, advance=100.0)

    job = cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert job["status"] == "running"
    with pytest.raises(typer.Exit) as exc:
        cli._emit_job(job, as_json=False)
    assert exc.value.exit_code == 4
    assert "agent-router wait job-1" in capsys.readouterr().err


def test_wait_zero_timeout_one_request() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway([make_job("running", text=None)], clock)

    job = cli._wait_until(gateway, "job-1", 0, clock=clock, sleep=clock.sleep)

    assert job["status"] == "running"
    assert gateway.calls == [("GET", "/api/v1/jobs/job-1/wait", {"timeout": 0.0})]


def test_wait_instant_running_no_busy_loop() -> None:
    clock = FakeClock()
    gateway = ScriptedGateway(None, clock)

    job = cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert job["status"] == "running"
    assert len(gateway.calls) <= 250
    assert len(clock.sleeps) >= len(gateway.calls) - 1
    assert set(clock.sleeps) == {1.0}


def test_queue_full_returns_six_with_specific_message(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    gateway.error = GatewayError(429, "queue_full", "queue is full, try again later")

    result = runner.invoke(cli.app, ["run", "--no-wait", _RUN_PROMPT])

    assert result.exit_code == 6
    assert "queue is full, try again later" in result.stderr


@pytest.mark.parametrize(
    "error",
    [
        GatewayUnavailable("gateway unavailable"),
        GatewayError(401, "unauthorized", "no token or it is invalid"),
        GatewayError(403, "forbidden", "admin token required"),
    ],
)
def test_unavailable_or_rejected_token_returns_five(
    runner: CliRunner, gateway: FakeGateway, error: Exception
) -> None:
    gateway.error = error

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 5


def test_unprocessable_request_returns_six(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.error = GatewayError(422, "bad_prompt", "empty prompt")

    result = runner.invoke(cli.app, ["run", "--no-wait", " "])

    assert result.exit_code == 6
    assert "empty prompt" in result.stderr


def test_run_sends_options_and_absolute_cwd(
    runner: CliRunner, gateway: FakeGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "каталог задачи"  # non-ascii on purpose
    work.mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "--cwd",
            "каталог задачи",  # non-ascii on purpose
            "--mode",
            "write",
            "--tier",
            "strong",
            "--caller",
            "agent-a",
            "--route",
            "fake/main",
            "--timeout",
            "45",
            "--no-wait",
            _RUN_PROMPT,
        ],
    )

    assert result.exit_code == 0
    assert gateway.calls[0][2] == {
        "prompt": _RUN_PROMPT,
        "cwd": str(work.resolve()),
        "mode": "write",
        "tier": "strong",
        "caller": "agent-a",
        "route": "fake/main",
        "timeout_s": 45,
    }
    assert gateway.tokens_used == [gateway.client_token]


def test_result_reads_job_with_client_token(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 0
    assert gateway.calls == [("GET", "/api/v1/jobs/job-1", {})]
    assert gateway.tokens_used == [gateway.client_token]


# --- ratings ------------------------------------------------------------------------


def test_rate_posts_rating_and_prints_summary(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(
        cli.app,
        [
            "rate",
            "job-1",
            "good",
            "--should-tier",
            "cheap",
            "--by",
            "agent-a",
            "--note",
            "a note",
        ],
    )

    assert result.exit_code == 0
    assert gateway.calls == [
        (
            "POST",
            "/api/v1/jobs/job-1/rating",
            {"value": "good", "should_tier": "cheap", "by": "agent-a", "note": "a note"},
        )
    ]
    assert gateway.tokens_used == [gateway.client_token]
    assert "rating recorded" in result.stdout
    assert "job-1" in result.stdout


def test_rate_sends_only_given_options(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["rate", "job-1", "bad"])

    assert result.exit_code == 0
    assert gateway.calls == [("POST", "/api/v1/jobs/job-1/rating", {"value": "bad"})]


def test_rate_conflict_returns_six(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.error = GatewayError(409, "job_not_finished", "job not finished yet: running")

    result = runner.invoke(cli.app, ["rate", "job-1", "good"])

    assert result.exit_code == 6
    assert "job not finished yet: running" in result.stderr


def test_result_hints_rate_for_unrated_job(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 0
    assert "rate: agent-router rate job-1 good|bad|partial" in result.stderr


def test_result_has_no_rate_hint_for_rated_job(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job(rating={"value": "good", "should_tier": None})

    result = runner.invoke(cli.app, ["result", "job-1"])

    assert result.exit_code == 0
    assert "rate:" not in result.stderr


def test_wait_hints_rate_for_unrated_job(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["wait", "job-1", "--timeout", "1"])

    assert result.exit_code == 0
    assert "rate: agent-router rate job-1 good|bad|partial" in result.stderr


def test_cancel_posts_action(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("cancelled", text=None)

    result = runner.invoke(cli.app, ["cancel", "job-1"])

    assert result.exit_code == 0
    assert gateway.calls == [("POST", "/api/v1/jobs/job-1/cancel", None)]
    assert result.stdout == "Job job-1: cancelled\n"


def test_cancel_of_running_job_says_it_is_stopping(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("running", text=None, cancel_requested=True)

    result = runner.invoke(cli.app, ["cancel", "job-1"])

    assert result.exit_code == 0
    assert result.stdout.startswith("Job job-1: cancel requested")
    assert "agent-router wait job-1" in result.stdout


def test_jobs_lists_route_tier_and_creation_time(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["jobs"])

    assert result.exit_code == 0
    created = format_local("2026-11-13T09:00:00+00:00")
    assert result.stdout.splitlines() == [f"job-1\tcompleted\tfake/main\tstandard\t{created}"]


def test_approve_names_the_next_step(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("queued", text=None, route=None, finished_at=None)

    result = runner.invoke(cli.app, ["approve", "job-1"])

    assert result.exit_code == 0
    assert "approved: follow the job with agent-router wait job-1" in result.stderr


def test_approve_of_a_finished_job_shows_no_next_step(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    result = runner.invoke(cli.app, ["approve", "job-1"])

    assert result.exit_code == 0
    assert "agent-router wait" not in result.stderr


def test_jobs_filters_by_status(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["jobs", "--status", "running"])

    assert result.exit_code == 0
    assert "job-1" in result.stdout
    assert gateway.calls == [("GET", "/api/v1/jobs", {"status": "running"})]


@pytest.mark.parametrize("action", ["approve", "deny"])
def test_approval_commands_use_admin_token(
    runner: CliRunner, gateway: FakeGateway, action: str
) -> None:
    result = runner.invoke(cli.app, [action, "job-1"])

    assert result.exit_code == 0
    assert gateway.calls == [("POST", f"/api/v1/jobs/job-1/{action}", None)]
    assert gateway.tokens_used == [gateway.admin_token]


def test_unblock_uses_admin_token(runner: CliRunner, gateway: FakeGateway) -> None:
    result = runner.invoke(cli.app, ["unblock", "pool-a"])

    assert result.exit_code == 0
    assert gateway.calls == [("POST", "/api/v1/pools/pool-a/unblock", None)]
    assert gateway.tokens_used == [gateway.admin_token]
    assert result.stdout == "Pool pool-a: unblocked\n"


def test_unblock_says_which_window_still_blocks(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.unblocked = {
        "blocked": True,
        "blocked_by": "5h",
        "blocked_until": "2026-11-13T12:00:00+00:00",
    }

    result = runner.invoke(cli.app, ["unblock", "pool-a"])

    assert result.exit_code == 0
    assert result.stdout.startswith("Pool pool-a: manual block removed, but window 5h is exhausted")
    assert "until" in result.stdout
    assert "the pool stays excluded" in result.stdout


def test_unblock_without_window_deadline(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.unblocked = {"blocked": True, "blocked_by": "week", "blocked_until": None}

    result = runner.invoke(cli.app, ["unblock", "pool-a"])

    assert result.exit_code == 0
    assert "window week is exhausted;" in result.stdout
    assert "exhausted until" not in result.stdout


# --- exit codes through the real client ---------------------------------------------------


@pytest.mark.parametrize("command", [["status"], ["result", "job-1"]])
@pytest.mark.parametrize(
    ("status", "body", "headers", "exit_code"),
    [
        (404, b'{"error": {"code": "not_found", "message": "x"}}', None, 6),
        (401, b'{"error": {"code": "unauthorized", "message": "x"}}', None, 5),
        (500, b"<html>", None, 1),
        (302, b"", {"Location": "http://127.0.0.1:9/x"}, 1),
        (200, b"<html>", None, 1),
    ],
)
def test_cli_exit_codes_through_real_client(
    runner: CliRunner,
    config,
    tokens,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    status: int,
    body: bytes,
    headers: dict[str, str] | None,
    exit_code: int,
) -> None:
    from helpers import http_server

    with http_server(status=status, body=body, headers=headers) as (url, seen):
        monkeypatch.setenv("AGENT_ROUTER_URL", url)
        result = runner.invoke(cli.app, command)
    assert result.exit_code == exit_code, result.output
    assert len(seen) == 1


@pytest.mark.parametrize("command", [["status"], ["result", "job-1"]])
def test_cli_closed_port_through_real_client_returns_five(
    runner: CliRunner, config, tokens, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    from helpers import closed_port_url

    monkeypatch.setenv("AGENT_ROUTER_URL", closed_port_url())
    assert runner.invoke(cli.app, command).exit_code == 5


# --- approve waits for a queued job -------------------------------------------------------


class ApprovalGateway:
    """Answers approve/deny with scripted errors before the final job."""

    def __init__(self, errors: list[Exception], clock: FakeClock, forever: bool = False) -> None:
        self.errors = errors
        self.clock = clock
        self.forever = forever
        self.calls: list[str] = []

    def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(path)
        if self.forever:
            raise self.errors[0]
        if self.errors:
            raise self.errors.pop(0)
        return make_job("queued", text=None)


def _queued() -> GatewayError:
    return GatewayError(409, "wrong_state", "job is not awaiting approval: queued")


def test_approve_retries_while_job_is_queued() -> None:
    clock = FakeClock()
    gateway = ApprovalGateway([_queued(), _queued()], clock)

    job = cli._approve_when_ready(gateway, "job-1", "approve", clock=clock, sleep=clock.sleep)

    assert job["status"] == "queued"
    assert gateway.calls == ["/api/v1/jobs/job-1/approve"] * 3
    assert clock.sleeps == [0.5, 0.5]


def test_approve_gives_up_after_deadline() -> None:
    clock = FakeClock()
    gateway = ApprovalGateway([_queued()], clock, forever=True)

    with pytest.raises(GatewayError) as caught:
        cli._approve_when_ready(gateway, "job-1", "deny", clock=clock, sleep=clock.sleep)

    assert caught.value.status == 409
    assert clock.now <= 10.5
    assert len(gateway.calls) > 2


@pytest.mark.parametrize(
    "error",
    [
        GatewayError(409, "wrong_state", "job is not awaiting approval: running"),
        GatewayError(409, "wrong_state", "approval deadline expired"),
        GatewayError(404, "not_found", "job not found: job-1"),
    ],
)
def test_approve_does_not_retry_other_errors(error: GatewayError) -> None:
    clock = FakeClock()
    gateway = ApprovalGateway([error], clock)

    with pytest.raises(GatewayError):
        cli._approve_when_ready(gateway, "job-1", "approve", clock=clock, sleep=clock.sleep)

    assert len(gateway.calls) == 1
    assert clock.sleeps == []


def test_run_stops_early_when_the_job_awaits_approval(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    gateway.job = make_job("awaiting_approval", text=None, finished_at=None)

    result = runner.invoke(cli.app, ["run", _RUN_PROMPT])

    assert result.exit_code == 4
    waits = [call for call in gateway.calls if call[1].endswith("/wait")]
    assert waits == [("GET", "/api/v1/jobs/job-1/wait", {"timeout": 2})]
    assert "job awaits approval: agent-router approve job-1" in result.stderr
    assert "still running" not in result.stderr


def test_run_keeps_waiting_while_the_job_runs(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("running", text=None)

    result = runner.invoke(cli.app, ["run", _RUN_PROMPT])

    assert result.exit_code == 4
    waits = [call[2] for call in gateway.calls if call[1].endswith("/wait")]
    assert waits == [{"timeout": 2}, {"timeout": 98}]
    assert "job still running; retry: agent-router wait job-1" in result.stderr


def test_wait_on_a_job_awaiting_approval_names_approve(
    runner: CliRunner, gateway: FakeGateway
) -> None:
    gateway.job = make_job("awaiting_approval", text=None, finished_at=None)

    result = runner.invoke(cli.app, ["wait", "job-1"])

    assert result.exit_code == 4
    assert "job awaits approval: agent-router approve job-1" in result.stderr


_DEADLINE = "2026-11-13T09:10:00+00:00"


def test_cli_wait_stops_on_awaiting_approval(capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    waiting = make_job(
        "awaiting_approval",
        text=None,
        finished_at=None,
        approval={"route": "fake/main", "deadline": _DEADLINE},
    )
    gateway = ScriptedGateway([waiting] * 5, clock)

    job = cli._wait_until(gateway, "job-1", 250, clock=clock, sleep=clock.sleep)

    assert job["status"] == "awaiting_approval"
    assert len(gateway.calls) == 1
    assert _emit_exit_code(job) == 4
    err = capsys.readouterr().err
    assert (
        f"job awaits approval: agent-router approve job-1 before {format_local(_DEADLINE)}, "
        "then agent-router wait job-1"
    ) in err


def test_cli_deny_names_denied_route(runner: CliRunner, gateway: FakeGateway) -> None:
    gateway.job = make_job("queued", text=None, finished_at=None, route="fake/main")

    result = runner.invoke(cli.app, ["deny", "job-1"])

    assert result.exit_code == 0
    assert "denied route fake/main: the job looks for another route" in result.stderr
