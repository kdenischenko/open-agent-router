"""Codex CLI adapter: command construction and parsing of recorded JSONL output."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import pytest

from agent_router.adapters.base import RunOutcome
from agent_router.registry import HARNESS_GROUP, load_harness
from agent_router.runner import ResolvedExecutable

FIXTURES = Path(__file__).parent / "fixtures"
EXE = ResolvedExecutable(Path("bin") / "codex.exe", False)
# start of the Russian answer in codex_read_blocked_write.jsonl
FAILED_WRITE_REPORT = "\u0421\u043e\u0437\u0434\u0430\u0442\u044c `x.txt`"
PROMPT_FILE = Path("jobs") / "j_1" / "prompt.md"
MODEL = "gpt-6.2-sol"


def adapter():
    return load_harness("codex").adapter


def sample(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_codex_registered() -> None:
    names = {entry.name for entry in metadata.entry_points(group=HARNESS_GROUP)}
    assert "codex" in names
    spec = load_harness("codex")
    assert spec.spend == "journal"
    assert spec.executables == ("codex",)
    assert spec.min_version == "0.172.0"
    assert spec.adapter.name == "codex"
    assert spec.adapter.reports_progress is True
    assert spec.adapter.quiet_while_working is True


@pytest.mark.parametrize(
    ("mode", "sandbox"),
    [("read", "read-only"), ("write", "workspace-write")],
)
def test_build_uses_stdin_and_mode_sandbox(mode: str, sandbox: str) -> None:
    inv = adapter().build("TEXT ONE", mode, MODEL, EXE, PROMPT_FILE)

    assert inv.argv == (
        str(EXE.path),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "-c",
        "windows.sandbox=unelevated",
        "-c",
        "approval_policy=never",
        "-m",
        MODEL,
        "-s",
        sandbox,
        "-",
    )
    assert inv.stdin_text == "TEXT ONE"
    assert "-C" not in inv.argv


def test_build_without_executable_fails() -> None:
    with pytest.raises(ValueError):
        adapter().build("TEXT", "read", MODEL, ResolvedExecutable(None, False), PROMPT_FILE)


def test_parse_read_ok_takes_last_agent_message() -> None:
    outcome = adapter().parse(sample("codex_read_ok.jsonl"), "Reading additional input...\n", 0)

    assert outcome == RunOutcome(
        "ok",
        text="hello",
        tokens_in=45525,
        tokens_out=55,
        session_id="01a22185-6678-7de0-8136-322168087e8b",
    )


def test_parse_write_ok() -> None:
    outcome = adapter().parse(sample("codex_write_ok.jsonl"), "", 0)

    assert outcome == RunOutcome(
        "ok",
        text="ok",
        tokens_in=46507,
        tokens_out=86,
        session_id="01a22185-8844-7150-a4e6-772eb9e9af4d",
    )


def test_parse_error_ignores_warning_item() -> None:
    outcome = adapter().parse(sample("codex_error.jsonl"), "", 1)

    assert outcome.status == "error"
    assert outcome.error == (
        "The 'no-such-model-x' model is not supported when using Codex with a ChatGPT account."
    )
    assert outcome.session_id == "01a22184-80b9-76f2-a00e-48144e256716"
    assert outcome.tokens_in is None and outcome.tokens_out is None


def test_parse_synthetic_limit_fixture() -> None:
    # Synthetic fixture in the recorded error shape; no live exhausted-plan capture exists
    outcome = adapter().parse(sample("codex_limit.jsonl"), "", 1)

    assert outcome.status == "limit"
    assert outcome.error is not None and outcome.error.startswith("You've hit your usage limit")
    assert outcome.limit_reset_at == datetime(2026, 11, 28, 14, 5, tzinfo=UTC)


def test_parse_blocked_write_in_read_mode_is_ok() -> None:
    outcome = adapter().parse(sample("codex_read_blocked_write.jsonl"), "", 0)

    assert outcome.status == "ok"
    assert outcome.text.startswith(FAILED_WRITE_REPORT)
    assert (outcome.tokens_in, outcome.tokens_out) == (45790, 110)


def test_parse_missing_usage_leaves_tokens_unknown() -> None:
    stdout = "\n".join(sample("codex_read_ok.jsonl").splitlines()[:-1])

    outcome = adapter().parse(stdout, "", 0)

    assert outcome.status == "ok"
    assert outcome.tokens_in is None and outcome.tokens_out is None


def test_final_line_is_completed_or_failed_turn() -> None:
    assert adapter().is_final_line('{"type":"turn.completed","usage":{}}')
    assert adapter().is_final_line('{"type":"turn.failed","error":{"message":"boom"}}')
    assert not adapter().is_final_line('{"type":"turn.started"}')
    assert not adapter().is_final_line('{"type":"item.completed","item":{}}')
    assert not adapter().is_final_line("not json")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('{"type":"item.started","item":{"type":"command_execution"}}', True),
        ('{"type":"item.completed","item":{"type":"agent_message"}}', True),
        ('{"type":"turn.started"}', True),
        ('{"type":"turn.completed","usage":{}}', True),
        ('{"type":"turn.failed","error":{}}', True),
        ('{"type":"thread.started","thread_id":"t"}', False),
        ('{"type":"error","message":"boom"}', False),
        ("plain output", False),
    ],
)
def test_progress_line_is_an_item_or_turn_event(line: str, expected: bool) -> None:
    assert adapter().is_progress_line(line) is expected
