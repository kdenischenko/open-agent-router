"""Harness adapters: exact commands and parsing of sanitized output samples."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import pytest

from agent_router.adapters import (
    MAX_ARG_CHARS,
    RunOutcome,
    looks_like_limit,
    with_preamble,
)
from agent_router.registry import HARNESS_GROUP, load_harness
from agent_router.runner import ResolvedExecutable, check_shim_args

FIXTURES = Path(__file__).parent / "fixtures"
EXE = ResolvedExecutable(Path("bin") / "tool.exe", False)
SHIM = ResolvedExecutable(Path("bin") / "tool.cmd", True)
PROMPT_FILE = Path("jobs") / "j_1" / "prompt.md"
RESET = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)

kimi = load_harness("kimi").adapter
opencode = load_harness("opencode").adapter
claude = load_harness("claude").adapter


def sample(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- preamble and limit signs ---------------------------------------------------------


def test_preamble_exact_text_for_write() -> None:
    assert with_preamble("do the thing", "write") == (
        "[agent-router] You are executing a delegated task for an orchestrator.\n"
        "Rules:\n"
        "- Work only inside the current working directory.\n"
        "- Do not run `git commit`, `git push`, or any command that rewrites git history.\n"
        "- Do not start long-running background processes.\n"
        "- When finished, reply with a concise final report of what you did or found.\n"
        "---\n"
        "do the thing"
    )


def test_preamble_read_adds_read_only_rule() -> None:
    read = with_preamble("look around", "read")
    write = with_preamble("look around", "write")
    rule = "- This is a read-only task: do not create, modify, or delete any files.\n---\n"
    assert rule in read
    assert "read-only" not in write
    assert read.endswith("---\nlook around")


@pytest.mark.parametrize(
    "text",
    [
        "Rate limit exceeded",
        "rate_limit_error",
        "usage limit reached",
        "Quota exhausted for this month",
        "limit reached, come back later",
        "You've hit your limit · resets 3am (Europe/Berlin)",
        "Too Many Requests",
        "HTTP 429",
    ],
)
def test_looks_like_limit_signs(text: str) -> None:
    assert looks_like_limit(text)


@pytest.mark.parametrize(
    "text", ["", "model unavailable", "error 4291", "port 14290 is busy", "no route to host"]
)
def test_looks_like_limit_ignores_other_errors(text: str) -> None:
    assert not looks_like_limit(text)


# --- command assembly -----------------------------------------------------------------


def test_registry_has_three_adapters() -> None:
    names = {entry.name for entry in metadata.entry_points(group=HARNESS_GROUP)}
    assert names >= {"kimi", "opencode", "claude"}
    assert {name: load_harness(name).adapter.name for name in ("kimi", "opencode", "claude")} == {
        "kimi": "kimi",
        "opencode": "opencode",
        "claude": "claude",
    }
    assert MAX_ARG_CHARS == 20_000


@pytest.mark.parametrize("mode", ["read", "write"])
def test_kimi_argv_is_same_in_both_modes(mode: str) -> None:
    inv = kimi.build("TEXT", mode, "kimi-code/k3.1-pro", EXE, PROMPT_FILE)
    assert inv.argv == (
        str(EXE.path),
        "-p",
        "TEXT",
        "-m",
        "kimi-code/k3.1-pro",
        "--output-format",
        "stream-json",
    )
    assert inv.stdin_text is None


def test_kimi_long_prompt_goes_by_file() -> None:
    text = "x" * (MAX_ARG_CHARS + 1)
    inv = kimi.build(text, "write", "kimi-code/k3.1-pro", EXE, PROMPT_FILE)
    assert inv.argv == (
        str(EXE.path),
        "-p",
        f"Read the task from the file {PROMPT_FILE.as_posix()} and execute it exactly.",
        "-m",
        "kimi-code/k3.1-pro",
        "--output-format",
        "stream-json",
        "--add-dir",
        PROMPT_FILE.parent.as_posix(),
    )
    assert inv.stdin_text is None
    assert text not in inv.argv


def test_kimi_prompt_of_exactly_max_chars_stays_argument() -> None:
    text = "x" * MAX_ARG_CHARS
    inv = kimi.build(text, "read", "m", EXE, PROMPT_FILE)
    assert inv.argv[2] == text and "--add-dir" not in inv.argv


def test_opencode_argv_uses_standalone_in_read_and_write() -> None:
    base = (
        str(EXE.path),
        "run",
        "--format",
        "json",
        "--archived",
        "--standalone",
        "--model",
        "prov/model-1",
    )
    read = opencode.build("TEXT", "read", "prov/model-1", EXE, PROMPT_FILE)
    write = opencode.build("TEXT", "write", "prov/model-1", EXE, PROMPT_FILE)
    assert read.argv == (*base, "--agent", "plan")
    assert write.argv == (*base, "--auto")
    assert read.stdin_text == "TEXT" and write.stdin_text == "TEXT"


def test_claude_argv_read_and_write() -> None:
    base = (
        str(EXE.path),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "sonnet",
    )
    read = claude.build("TEXT", "read", "sonnet", EXE, PROMPT_FILE)
    write = claude.build("TEXT", "write", "sonnet", EXE, PROMPT_FILE)
    isolation = ("--setting-sources", "project", "--strict-mcp-config")
    assert read.argv == (*base, "--permission-mode", "plan", *isolation)
    assert write.argv == (*base, "--permission-mode", "bypassPermissions", *isolation)
    assert read.stdin_text == "TEXT" and write.stdin_text == "TEXT"


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("exe", [EXE, SHIM], ids=["exe", "shim"])
def test_prompt_is_never_a_separate_option(name: str, mode: str, exe: ResolvedExecutable) -> None:
    inv = load_harness(name).adapter.build("--auto", mode, "model", exe, PROMPT_FILE)
    for index, arg in enumerate(inv.argv):
        if arg != "--auto":
            continue
        if name == "opencode" and mode == "write" and index == len(inv.argv) - 1:
            continue  # own write-mode flag, not the prompt
        assert name == "kimi" and inv.argv[index - 1] == "-p" and index == 2


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
@pytest.mark.parametrize("mode", ["read", "write"])
def test_shim_never_gets_prompt_text_in_argv(name: str, mode: str) -> None:
    text = with_preamble('dangerous & text "in quotes" %PATH%', mode)
    inv = load_harness(name).adapter.build(text, mode, "prov/model-1.5", SHIM, PROMPT_FILE)
    assert all(text not in arg and "dangerous" not in arg for arg in inv.argv)
    check_shim_args(inv.argv)  # all shim arguments are safe for cmd.exe
    if name == "kimi":
        assert "--add-dir" in inv.argv and inv.stdin_text is None
    else:
        assert inv.stdin_text == text


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
def test_build_without_executable_fails(name: str) -> None:
    with pytest.raises(ValueError):
        load_harness(name).adapter.build(
            "TEXT", "read", "m", ResolvedExecutable(None, False), PROMPT_FILE
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("claude", True), ("kimi", True), ("opencode", True), ("cline", True)],
)
def test_adapters_declare_progress_reporting(name: str, expected: bool) -> None:
    assert load_harness(name).adapter.reports_progress is expected


# --- parsing: kimi --------------------------------------------------------------------


def test_kimi_ok() -> None:
    outcome = kimi.parse(sample("kimi_ok.jsonl"), "", 0)
    assert outcome == RunOutcome("ok", text="done", session_id="session_<id>")


def test_kimi_multi_takes_last_assistant_text() -> None:
    outcome = kimi.parse(sample("kimi_multi.jsonl"), "", 0)
    assert outcome.status == "ok" and outcome.text == "pineapple"
    assert outcome.tokens_in is None and outcome.tokens_out is None and outcome.cost_usd is None


def test_kimi_error_exit_code() -> None:
    outcome = kimi.parse("", "error: Cannot combine --prompt with --plan.\n", 1)
    assert outcome.status == "error"
    assert outcome.error == "error: Cannot combine --prompt with --plan."
    assert outcome.limit_reset_at is None


def test_kimi_limit_sample() -> None:
    outcome = kimi.parse("", sample("limit_kimi.txt"), 1)
    assert outcome.status == "limit"
    assert outcome.limit_reset_at == RESET
    assert outcome.error


def test_kimi_limit_in_non_json_stdout_line() -> None:
    outcome = kimi.parse("Error: 429 Too Many Requests\n", "", 1)
    assert outcome.status == "limit" and outcome.limit_reset_at is None


def test_kimi_final_line() -> None:
    lines = sample("kimi_ok.jsonl").splitlines()
    assert kimi.is_final_line(lines[-1])
    assert not any(kimi.is_final_line(line) for line in lines[:-1])
    assert not kimi.is_final_line('{"role":"assistant","content":"session.resume_hint"}')
    assert not kimi.is_final_line("session.resume_hint")
    assert not opencode.is_final_line(lines[-1]) and not claude.is_final_line(lines[-1])


# --- parsing: opencode ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('{"type":"retry","error":"ECONNRESET"}', False),
        ('  {"type":"retry"}  ', False),
        ('{"type":"step_start"}', True),
        ('{"type":"step_finish"}', True),
        ('{"type":"tool_use"}', True),
        ('{"type":"text","part":{"text":"retry"}}', True),
        ('{"type":"unknown"}', True),
        ('{"type":"retrying"}', True),
        ("plain output", True),
        ("not json: {", True),
    ],
)
def test_opencode_is_progress_line(line: str, expected: bool) -> None:
    assert opencode.is_progress_line(line) is expected


def test_opencode_ok() -> None:
    outcome = opencode.parse(sample("opencode_ok.jsonl"), "", 0)
    assert outcome == RunOutcome(
        "ok", text="pineapple", tokens_in=9067, tokens_out=21, session_id="ses_<id>"
    )


def test_opencode_multi_step_sums_tokens_and_takes_last_message() -> None:
    outcome = opencode.parse(sample("opencode_multi_step.jsonl"), "", 0)
    assert outcome.status == "ok"
    assert outcome.text == "done"  # first message text is not part of the reply
    assert outcome.tokens_in == 9079 + 2563
    assert outcome.tokens_out == 18 + 3
    assert outcome.cost_usd is None


def test_opencode_text_parts_of_last_message_are_joined() -> None:
    def text(message: str, value: str) -> str:
        return json.dumps({"type": "text", "part": {"messageID": message, "text": value}})

    stdout = "\n".join([text("m1", "one"), text("m2", "two"), text("m2", "three")])
    assert opencode.parse(stdout, "", 0).text == "two\nthree"


def test_opencode_error_event() -> None:
    outcome = opencode.parse(sample("opencode_error.jsonl"), "", 1)
    assert outcome.status == "error"
    assert outcome.error == "provider.no-route: Model unavailable: opencode-go/no-such-model"
    assert outcome.session_id == "ses_<id>"


def test_opencode_limit_sample_by_error_type() -> None:
    outcome = opencode.parse(sample("limit_opencode.txt"), "", 1)
    assert outcome.status == "limit"
    assert outcome.error is not None and outcome.error.startswith("provider.quota-exceeded")


def test_opencode_limit_by_error_message() -> None:
    line = json.dumps(
        {"type": "error", "error": {"type": "provider.http", "message": "429 Too Many Requests"}}
    )
    assert opencode.parse(line + "\n", "", 1).status == "limit"


# --- parsing: claude ------------------------------------------------------------------


def test_claude_ok() -> None:
    outcome = claude.parse(sample("claude_ok.json"), "", 0)
    assert outcome == RunOutcome(
        "ok",
        text="pineapple",
        tokens_in=1200 + 8000,
        tokens_out=45,
        cost_usd=0.0123,
        session_id="<session id>",
    )


def test_claude_error() -> None:
    outcome = claude.parse(sample("claude_error.json"), "", 1)
    assert outcome.status == "error" and outcome.text == ""
    assert outcome.error == "Invalid model name: no-such-model"


def test_claude_is_error_wins_over_zero_exit_code() -> None:
    assert claude.parse(sample("claude_error.json"), "", 0).status == "error"


def test_claude_limit_sample() -> None:
    outcome = claude.parse(sample("limit_claude.txt"), "", 1)
    assert outcome.status == "limit" and outcome.limit_reset_at == RESET


def test_claude_stream_read_includes_cache_tokens_and_allows_rate_event() -> None:
    outcome = claude.parse(sample("claude_stream_read.jsonl"), "", 0)
    assert outcome == RunOutcome(
        "ok",
        text="ok",
        tokens_in=2 + 23429 + 26344,
        tokens_out=4,
        cost_usd=0.09639439999999999,
        session_id="<session_id>",
    )


def test_claude_stream_write_takes_result_before_trailing_system_event() -> None:
    outcome = claude.parse(sample("claude_stream_write.jsonl"), "", 0)
    assert outcome.status == "ok" and outcome.text == "The command printed `42`."
    assert outcome.tokens_in == 4 + 10204 + 62487
    assert outcome.tokens_out == 385
    assert outcome.cost_usd == 0.0028585700000000004


def test_claude_stream_ignores_result_fields_on_other_event_types() -> None:
    events = [
        {"type": "result", "is_error": False, "result": "right"},
        {"type": "system", "is_error": True, "result": "wrong"},
    ]
    stdout = "\n".join(json.dumps(event) for event in events)
    outcome = claude.parse(stdout, "", 0)
    assert outcome.status == "ok" and outcome.text == "right"


def test_claude_final_line_is_result_event() -> None:
    lines = sample("claude_stream_write.jsonl").splitlines()
    assert [claude.is_final_line(line) for line in lines] == [
        *([False] * (len(lines) - 2)),
        True,
        False,
    ]
    assert not claude.is_final_line("not json")
    assert not claude.is_final_line('{"result":"old format without an event type"}')


@pytest.mark.parametrize(
    ("result_fields", "rate_limit_status", "expected_reset"),
    [
        ({"api_error_status": 429}, "allowed", None),
        ({"api_error_type": "rate_limit_error"}, "allowed", None),
        ({}, "rejected", datetime.fromtimestamp(1795389000, UTC)),
    ],
)
def test_claude_stream_limit_detection(
    result_fields: dict, rate_limit_status: str, expected_reset: datetime | None
) -> None:
    # Synthetic input from the observed event schemas; no live exhausted-plan capture exists
    events = [
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": rate_limit_status, "resetsAt": 1795389000},
        },
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "You've hit your limit · resets 3am (Europe/Berlin)",
            **result_fields,
        },
    ]
    stdout = "\n".join(json.dumps(event) for event in events)
    outcome = claude.parse(stdout, "", 1)
    assert outcome.status == "limit"
    assert outcome.error == "You've hit your limit · resets 3am (Europe/Berlin)"
    assert outcome.limit_reset_at == expected_reset


# --- shared cases ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
def test_empty_output_with_zero_code_is_error(name: str) -> None:
    outcome = load_harness(name).adapter.parse("", "", 0)
    assert outcome.status == "error" and outcome.error == "empty output"


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
def test_broken_output_is_skipped(name: str) -> None:
    broken = '{"role": "assist\nnot json at all\n[1, 2]\n{"type": "text", "part": 5}\n\n'
    outcome = load_harness(name).adapter.parse(broken, "", 0)
    assert outcome.status == "error" and outcome.error == "empty output"
    failed = load_harness(name).adapter.parse(broken, "", 3)
    assert failed.status == "error" and failed.error


@pytest.mark.parametrize("name", ["kimi", "opencode", "claude"])
def test_no_exit_code_is_error(name: str) -> None:
    outcome = load_harness(name).adapter.parse("", "", None)
    assert outcome.status == "error" and outcome.error == "process did not exit"


ANSWER_ABOUT_LIMITS = "Use a rate limit: on quota errors and HTTP 429 back off and retry."


def test_answer_mentioning_limits_is_ok_kimi() -> None:
    stdout = json.dumps({"role": "assistant", "content": ANSWER_ABOUT_LIMITS}) + "\n"
    outcome = kimi.parse(stdout, "", 0)
    assert outcome.status == "ok" and outcome.text == ANSWER_ABOUT_LIMITS


def test_answer_mentioning_limits_is_ok_opencode() -> None:
    stdout = json.dumps(
        {"type": "text", "part": {"messageID": "m1", "type": "text", "text": ANSWER_ABOUT_LIMITS}}
    )
    outcome = opencode.parse(stdout + "\n", "", 0)
    assert outcome.status == "ok" and outcome.text == ANSWER_ABOUT_LIMITS


def test_answer_mentioning_limits_is_ok_claude() -> None:
    stdout = json.dumps({"result": ANSWER_ABOUT_LIMITS, "is_error": False})
    outcome = claude.parse(stdout, "", 0)
    assert outcome.status == "ok" and outcome.text == ANSWER_ABOUT_LIMITS


def test_failed_run_with_answer_about_limits_is_error_not_limit() -> None:
    # limit signs are matched only in service messages, not in the model's reply text
    stdout = json.dumps({"role": "assistant", "content": ANSWER_ABOUT_LIMITS}) + "\n"
    outcome = kimi.parse(stdout, "boom\n", 1)
    assert outcome.status == "error" and outcome.error == "boom"
