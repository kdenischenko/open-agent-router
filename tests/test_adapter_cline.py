"""Cline CLI adapter: command construction and parsing of anonymized `--json` output."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

import pytest

from agent_router.adapters.base import EMPTY_OUTPUT, MAX_ARG_CHARS, with_preamble
from agent_router.registry import HARNESS_GROUP, load_harness
from agent_router.runner import ResolvedExecutable, check_shim_args
from test_dispatcher import Step
from test_spend import new_spend_rig

FIXTURES = Path(__file__).parent / "fixtures"
EXE = ResolvedExecutable(Path("bin") / "cline-cli.exe", False)
SHIM = ResolvedExecutable(Path("bin") / "cline.cmd", True)
PROMPT_FILE = Path("jobs") / "j_1" / "prompt.md"
MODEL = "opencode-go/glm-5.3"


def adapter():
    return load_harness("cline").adapter


def sample(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cline_registered() -> None:
    names = {entry.name for entry in metadata.entry_points(group=HARNESS_GROUP)}
    assert "cline" in names
    spec = load_harness("cline")
    assert spec.spend == "journal"
    assert spec.executables == ("cline", "cline-cli")
    assert spec.proxy_capable is False
    assert spec.adapter.name == "cline"


def test_build_read_uses_plan() -> None:
    inv = adapter().build("TEXT ONE", "read", MODEL, EXE, PROMPT_FILE)
    assert "--plan" in inv.argv
    assert "--auto-approve" not in inv.argv
    assert "--yolo" not in inv.argv
    assert "--json" in inv.argv


def test_build_write_auto_approve_no_yolo() -> None:
    inv = adapter().build("TEXT ONE", "write", MODEL, EXE, PROMPT_FILE)
    index = inv.argv.index("--auto-approve")
    assert inv.argv[index + 1] == "true"
    assert "--plan" not in inv.argv
    assert "--yolo" not in inv.argv
    assert "--json" in inv.argv


@pytest.mark.parametrize(
    ("model", "provider", "name"),
    [("opencode-go/glm-5.3", "opencode-go", "glm-5.3"), ("opencode-go/a/b", "opencode-go", "a/b")],
)
def test_build_splits_provider_and_model(model: str, provider: str, name: str) -> None:
    argv = adapter().build("TEXT ONE", "read", model, EXE, PROMPT_FILE).argv
    assert argv[argv.index("-P") + 1] == provider
    assert argv[argv.index("-m") + 1] == name
    assert argv[0] == str(EXE.path)


@pytest.mark.parametrize("model", ["glm-5.3", "/glm-5.3", "opencode-go/"])
def test_build_model_without_provider_fails(model: str) -> None:
    with pytest.raises(ValueError) as caught:
        adapter().build("TEXT ONE", "read", model, EXE, PROMPT_FILE)
    assert "cline model must be <provider>/<model>" in str(caught.value)


def test_build_without_executable_fails() -> None:
    with pytest.raises(ValueError):
        adapter().build("TEXT ONE", "read", MODEL, ResolvedExecutable(None, False), PROMPT_FILE)


@pytest.mark.parametrize("mode", ["read", "write"])
def test_build_prompt_never_in_argv_before_separator(mode: str) -> None:
    text = with_preamble('--plan "in quotes" & %PATH%', mode)
    inv = adapter().build(text, mode, MODEL, EXE, PROMPT_FILE)
    assert inv.argv[-2:] == ("--", text)
    assert inv.argv.count("--") == 1
    assert all(text not in arg for arg in inv.argv[:-1])
    assert inv.stdin_text is None


@pytest.mark.parametrize("mode", ["read", "write"])
def test_shim_gets_file_phrase_not_text(mode: str) -> None:
    text = with_preamble('dangerous & text "in quotes" %PATH%', mode)
    inv = adapter().build(text, mode, MODEL, SHIM, PROMPT_FILE)
    assert all("dangerous" not in arg for arg in inv.argv)
    assert inv.argv[-2] == "--"
    assert PROMPT_FILE.as_posix() in inv.argv[-1]
    check_shim_args(inv.argv)


def test_long_prompt_goes_by_file() -> None:
    text = "x " * MAX_ARG_CHARS
    inv = adapter().build(text, "write", MODEL, EXE, PROMPT_FILE)
    assert all(text not in arg for arg in inv.argv)
    assert PROMPT_FILE.as_posix() in inv.argv[-1]


def test_prompt_of_exactly_max_chars_stays_argument() -> None:
    text = "y" * MAX_ARG_CHARS
    inv = adapter().build(text, "read", MODEL, EXE, PROMPT_FILE)
    assert inv.argv[-1] == text


def test_parse_ok_fixture() -> None:
    outcome = adapter().parse(sample("cline_ok.jsonl"), "", 0)
    assert outcome.status == "ok"
    assert outcome.text == "finished"
    assert (outcome.tokens_in, outcome.tokens_out) == (28898, 162)
    assert outcome.cost_usd is None  # opencode-go reports totalCost 0 — cost is unknown
    assert outcome.session_id == "conv_0001"


def test_parse_positive_cost_is_kept() -> None:
    lines = sample("cline_ok.jsonl").splitlines()
    last = json.loads(lines[-1])
    last["usage"]["totalCost"] = 0.0123
    stdout = "\n".join([*lines[:-1], json.dumps(last)])
    assert adapter().parse(stdout, "", 0).cost_usd == pytest.approx(0.0123)


def test_parse_error_fixture() -> None:
    outcome = adapter().parse(sample("cline_error.jsonl"), "", 1)
    assert outcome.status == "error"
    assert "Invalid credential" in (outcome.error or "")
    assert outcome.session_id == "conv_0001"


def test_parse_error_finish_reason_wins_over_zero_exit_code() -> None:
    outcome = adapter().parse(sample("cline_error.jsonl"), "", 0)
    assert outcome.status == "error"
    assert "Invalid credential" in (outcome.error or "")


def test_parse_top_level_error_line() -> None:
    stdout = json.dumps({"type": "error", "message": 'Unknown or disabled provider "x".'})
    outcome = adapter().parse(stdout + "\n", "", 1)
    assert outcome.status == "error"
    assert 'Unknown or disabled provider "x".' in (outcome.error or "")


def test_parse_limit() -> None:
    outcome = adapter().parse(sample("cline_limit.txt"), "", 1)
    assert outcome.status == "limit"
    assert "429" in (outcome.error or "")


def test_parse_empty() -> None:
    outcome = adapter().parse("", "", 0)
    assert outcome.status == "error"
    assert outcome.error == EMPTY_OUTPUT


def test_parse_plain_text_failure_keeps_stderr_and_limit() -> None:
    # without JSON and a non-zero exit code the diagnosis comes from stderr, not "empty output"
    limit = adapter().parse("", "429 Too Many Requests: usage limit reached", 1)
    assert limit.status == "limit"
    failure = adapter().parse("", "boom: connection refused", 1)
    assert failure.status == "error"
    assert failure.error == "boom: connection refused"


def test_parse_broken_json_nonzero_exit() -> None:
    outcome = adapter().parse("not json at all\n{broken\n", "", 1)
    assert outcome.status == "error"
    assert outcome.error


def test_answer_mentioning_limits_is_ok() -> None:
    lines = sample("cline_ok.jsonl").splitlines()
    last = json.loads(lines[-1])
    last["text"] = "rate limit and quota explained"
    stdout = "\n".join([*lines[:-1], json.dumps(last)])
    outcome = adapter().parse(stdout, "", 0)
    assert outcome.status == "ok"
    assert outcome.text == "rate limit and quota explained"


def test_cline_job_writes_spend(tmp_path, home, store, jobstore, clock, register_harness) -> None:
    rig = new_spend_rig(
        tmp_path,
        home,
        store,
        jobstore,
        clock,
        register_harness,
        adapter(),
        backend="cline",
        model=MODEL,
    )
    try:
        rig.runner.add(Step(sample("cline_ok.jsonl")))
        job = rig.submit(rig.plain())
        rig.tick(2)
        assert rig.status(job) == "completed"
        (row,) = rig.rows()
        assert (row["ref_id"], row["ref_n"], row["source"]) == (job.id, 1, "job")
        assert (row["tokens_in"], row["tokens_out"]) == (28898, 162)
        assert row["cost_usd"] is None  # totalCost 0 from opencode-go — cost is unknown
    finally:
        rig.close()
