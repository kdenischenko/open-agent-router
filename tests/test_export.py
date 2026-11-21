"""Export of the gateway event log: allowed fields only, no job texts, read-only DB access."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router.cli import app
from agent_router.config import effort_of, model_key
from agent_router.export import ExportError, export
from agent_router.jobs import Attempt, AttemptResult, Job, JobRequest, Rating
from agent_router.jobstore import SqliteJobStore
from agent_router.policy import PoolBlock
from agent_router.spend import SpendEntry
from agent_router.store import CounterSample, Observation, SqliteStore

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
# strings are built by concatenation so the repo hygiene test doesn't flag them as real
SECRET_PATH = "C:/" + "Users/x/secret"
TOKEN = "123456789:AA" + "x" * 30
FORBIDDEN = ("Users", "123456789:AA", "x" * 30)

JOB_KEYS = {
    "id",
    "status",
    "tier",
    "tier_source",
    "mode",
    "kind",
    "caller",
    "route",
    "reason",
    "had_refusal",
    "excluded_routes",
    "excluded_pools",
    "cancel_requested",
    "violations",
    "error_code",
    "classifier_tier",
    "classifier_confidence",
    "prompt_sha256",
    "prompt_chars",
    "created_at",
    "finished_at",
    "timeout_s",
    "changed_count",
    "error_chars",
    "repo",
    "cwd",
    "attempts",
    "rating",
    "votes",
}
ATTEMPT_KEYS = {
    "n",
    "route",
    "pool",
    "started_at",
    "finished_at",
    "outcome",
    "duration_s",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "cost_estimated",
    "spend_source",
    "decision",
    "effort",
    "limit_reset_at",
    "head_moved",
    "changes_unknown",
    "changed_count",
}
RATING_KEYS = {"value", "should_tier", "by", "at", "note_chars"}
SPEND_KEYS = {
    "pool",
    "at",
    "source",
    "route",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "estimated",
    "prices_version",
    "ref_id",
    "ref_n",
}
OBSERVATION_KEYS = {"pool", "window", "fraction", "observed_at", "resets_at", "source"}
COUNTER_KEYS = {"pool", "value_usd", "sampled_at", "scale_hash"}
BLOCK_KEYS = {"pool", "until", "reason", "job_id"}


def decision(model: str | None, route: str = "r-go") -> dict:
    chosen = None if model is None else {"route": route, "model": model, "score": 1.0}
    return {
        "at": T0.isoformat(),
        "tier": "standard",
        "tier_source": "default",
        "mode": "write",
        "caller": "claude-code",
        "chosen": chosen,
        "candidates": [] if chosen is None else [chosen],
        "excluded": [{"route": "r-off", "reason": "пул закрыт"}],  # non-ascii on purpose
    }


def make_job(job_id: str, *, created_at: datetime = T0, cwd: str = SECRET_PATH) -> Job:
    secret = f"{SECRET_PATH} {TOKEN}"
    return Job(
        id=job_id,
        request=JobRequest(
            prompt=f"сделай {secret}",  # non-ascii on purpose
            cwd=Path(cwd),
            mode="write",
            tier="standard",
            kind="code",
            caller="claude-code",
            timeout_s=600,
        ),
        status="completed",
        tier="standard",
        tier_source="caller",
        repo_root=Path(cwd),
        route="r-go",
        reason="выбран r-go",  # non-ascii on purpose
        text=f"ответ {secret}",  # non-ascii on purpose
        changed_files=(f"{SECRET_PATH}/a.py", f"{TOKEN}.txt"),
        violations=("head_moved",),
        error_code="harness_failed",
        error_message=f"ошибка {secret}",  # non-ascii on purpose
        created_at=created_at,
        finished_at=created_at + timedelta(minutes=5),
        prompt_sha256="ab" * 32,
        prompt_chars=42,
    )


def make_attempt(job_id: str, n: int, *, route: str = "r-go", model: str | None = None) -> Attempt:
    secret = f"{SECRET_PATH} {TOKEN}"
    started = T0 + timedelta(minutes=n)
    return Attempt(
        job_id=job_id,
        n=n,
        route=route,
        pool="opencode-go",
        started_at=started,
        finished_at=started + timedelta(seconds=30),
        outcome="ok",
        duration_s=30.0,
        tokens_in=100,
        tokens_out=50,
        cost_usd=None,
        head_before="0" * 40,
        tree_before={f"{SECRET_PATH}/a.py": ("h", 1, 2), f"{TOKEN}.txt": ("h", 3, 4)},
        log_tail=f"лог {secret}",  # non-ascii on purpose
        pid=4242,
        exe_name="opencode.exe",
        result=AttemptResult(
            "ok",
            text=f"ответ {secret}",  # non-ascii on purpose
            error=f"ошибка {secret}",  # non-ascii on purpose
            limit_reset_at=T0 + timedelta(hours=1),
            changed=(f"{SECRET_PATH}/a.py",),
            head_moved=True,
        ),
        spend_source="tokens",
        decision=None if model is None else decision(model, route),
    )


def open_db(home: Path) -> tuple[SqliteStore, SqliteJobStore]:
    store = SqliteStore(home / "agent-router.sqlite")
    return store, SqliteJobStore(store)


def fill(home: Path) -> Path:
    """Database with one job, two attempts, a rating, and rows in every table; closed."""
    store, jobs = open_db(home)
    try:
        jobs.create(make_job("j_secret"))
        jobs.add_attempt(make_attempt("j_secret", 1, model="opencode-go/glm-5.3#high"))
        jobs.add_attempt(make_attempt("j_secret", 2, model="opencode-go/glm-5.3"))
        jobs.add_rating(Rating("j_secret", "bad", "cheap", "claude-code", "cd" * 32, 17, T0))
        jobs.add_rating(Rating("j_secret", "good", None, "claude-code", None, None, T0))
        jobs.add_spend(
            SpendEntry("opencode-go", T0, "job", "r-go", 100, 50, None, False, None, "j_secret", 1)
        )
        store.add_observation(Observation("claude-pro", "5h", 0.4, T0, None, "manual"))
        store.add_counter_sample(CounterSample("opencode-go", 1.5, T0))
        jobs.add_block(PoolBlock("opencode-go", T0 + timedelta(hours=1), "limit"), "j_secret")
    finally:
        store.close()
    return home / "agent-router.sqlite"


def read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def run_export(db: Path, out: Path, fmt: str = "jsonl", **kwargs) -> list[Path]:
    kwargs.setdefault("since", T0 - timedelta(days=1))
    kwargs.setdefault("until", None)
    return export(db, out, fmt=fmt, **kwargs)  # type: ignore[arg-type]


# --- model name --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route_model", "key"),
    [
        ("kimi-code/k3.1-pro", "k3.1-pro"),
        ("opencode-go/glm-5.3#high", "glm-5.3"),
        ("glm-5.3", "glm-5.3"),
        ("glm-5.3#low", "glm-5.3"),
        ("openrouter/meta/llama-4", "meta/llama-4"),
    ],
)
def test_model_key(route_model: str, key: str) -> None:
    assert model_key(route_model) == key


@pytest.mark.parametrize(
    ("route_model", "effort"),
    [
        ("opencode-go/glm-5.3#high", "high"),
        ("opencode-go/glm-5.3", None),
        ("glm-5.3#", None),
        ("k3.1-pro#low", "low"),
    ],
)
def test_effort_from_model(route_model: str, effort: str | None) -> None:
    assert effort_of(route_model) == effort


# --- export contents -----------------------------------------------------------------


def test_export_jsonl_files_and_fields(tmp_path: Path) -> None:
    db = fill(tmp_path / "home")
    out = tmp_path / "out"
    paths = run_export(db, out)
    names = {path.name for path in paths}
    assert names == {
        "jobs.jsonl",
        "spend.jsonl",
        "observations.jsonl",
        "counter_samples.jsonl",
        "pool_blocks.jsonl",
    }
    assert all(path.parent == out and path.is_file() for path in paths)

    [job] = read_jsonl(out / "jobs.jsonl")
    assert set(job) == JOB_KEYS
    assert job["id"] == "j_secret"
    assert job["changed_count"] == 2
    assert job["error_chars"] == len(f"ошибка {SECRET_PATH} {TOKEN}")  # non-ascii on purpose
    assert job["cwd"]["name"] == "secret"
    assert len(job["cwd"]["hash"]) == 12
    assert job["repo"] == job["cwd"]
    assert [a["n"] for a in job["attempts"]] == [1, 2]
    for attempt in job["attempts"]:
        assert set(attempt) == ATTEMPT_KEYS
        assert attempt["changed_count"] == 1
        assert attempt["head_moved"] is True
    assert job["attempts"][0]["decision"]["chosen"]["route"] == "r-go"
    assert set(job["rating"]) == RATING_KEYS
    assert job["rating"]["value"] == "good"  # latest rating is exported whole
    assert job["rating"]["should_tier"] is None

    [spend] = read_jsonl(out / "spend.jsonl")
    assert set(spend) == SPEND_KEYS
    [obs] = read_jsonl(out / "observations.jsonl")
    assert set(obs) == OBSERVATION_KEYS
    [sample] = read_jsonl(out / "counter_samples.jsonl")
    assert set(sample) == COUNTER_KEYS
    [block] = read_jsonl(out / "pool_blocks.jsonl")
    assert set(block) == BLOCK_KEYS
    assert block["job_id"] == "j_secret"


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_export_has_no_texts(tmp_path: Path, fmt: str) -> None:
    db = fill(tmp_path / "home")
    out = tmp_path / "out"
    paths = run_export(db, out, fmt=fmt)
    files = sorted(out.iterdir())
    assert files and set(files) == set(paths)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            assert needle not in text, f"{path.name} contains {needle!r}"
        # and in the form Windows writes the path: with and without JSON escaping
        assert "Users\\\\x" not in text and "Users\\x" not in text


def test_export_salt_stable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = fill(home)
    store, jobs = open_db(home)
    try:
        jobs.create(make_job("j_other", cwd=SECRET_PATH + "2"))
    finally:
        store.close()
    run_export(db, tmp_path / "a")
    run_export(db, tmp_path / "b")
    first = {job["id"]: job["cwd"]["hash"] for job in read_jsonl(tmp_path / "a" / "jobs.jsonl")}
    second = {job["id"]: job["cwd"]["hash"] for job in read_jsonl(tmp_path / "b" / "jobs.jsonl")}
    assert first == second
    assert first["j_secret"] != first["j_other"]
    with sqlite3.connect(db) as conn:
        [(salt,)] = conn.execute("SELECT value FROM settings WHERE key = 'export_salt'")
    assert len(salt) == 32
    int(salt, 16)


def test_export_since_until(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store, jobs = open_db(home)
    try:
        for job_id, shift in (("j_before", -1), ("j_inside", 0), ("j_after", 1)):
            moment = T0 + timedelta(days=shift)
            jobs.create(make_job(job_id, created_at=moment))
            jobs.add_spend(
                SpendEntry("opencode-go", moment, "job", "r-go", 1, 1, None, False, None, job_id, 1)
            )
            store.add_observation(
                Observation("claude-pro", "5h", 0.1 + 0.1 * (shift + 1), moment, None, "manual")
            )
            store.add_counter_sample(CounterSample("opencode-go", 1.0 + shift + 1, moment))
    finally:
        store.close()
    out = tmp_path / "out"
    run_export(home / "agent-router.sqlite", out, since=T0, until=T0 + timedelta(days=1))
    assert [job["id"] for job in read_jsonl(out / "jobs.jsonl")] == ["j_inside"]
    assert [row["ref_id"] for row in read_jsonl(out / "spend.jsonl")] == ["j_inside"]
    assert len(read_jsonl(out / "observations.jsonl")) == 1
    assert len(read_jsonl(out / "counter_samples.jsonl")) == 1

    open_end = tmp_path / "open"
    run_export(home / "agent-router.sqlite", open_end, since=T0, until=None)
    ids = [job["id"] for job in read_jsonl(open_end / "jobs.jsonl")]
    assert ids == ["j_inside", "j_after"]


def test_export_csv_one_row_per_attempt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = fill(home)
    store, jobs = open_db(home)
    try:
        jobs.create(make_job("j_empty", created_at=T0 + timedelta(hours=1)))
    finally:
        store.close()
    out = tmp_path / "out"
    paths = run_export(db, out, fmt="csv")
    assert {path.name for path in paths} == {
        "jobs.csv",
        "spend.csv",
        "observations.csv",
        "counter_samples.csv",
        "pool_blocks.csv",
    }
    rows = read_csv(out / "jobs.csv")
    assert [(row["id"], row["attempt_n"]) for row in rows] == [
        ("j_secret", "1"),
        ("j_secret", "2"),
        ("j_empty", ""),
    ]
    first = rows[0]
    assert first["cwd_name"] == "secret"
    assert len(first["cwd_hash"]) == 12
    assert first["rating_value"] == "good"
    assert json.loads(first["attempt_decision"])["chosen"]["route"] == "r-go"
    assert json.loads(first["violations"]) == ["head_moved"]
    assert len(read_csv(out / "spend.csv")) == 1


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_export_csv_neutralises_formulas(tmp_path: Path, lead: str) -> None:
    home = tmp_path / "home"
    store, jobs = open_db(home)
    try:
        job = make_job("j_formula")
        job.reason = f"{lead}HYPERLINK(1)"
        jobs.create(job)
        jobs.add_block(PoolBlock("opencode-go", T0 + timedelta(hours=1), f"{lead}cmd"), "j_formula")
    finally:
        store.close()
    db = home / "agent-router.sqlite"

    run_export(db, tmp_path / "c", fmt="csv")
    assert read_csv(tmp_path / "c" / "jobs.csv")[0]["reason"] == f"'{lead}HYPERLINK(1)"
    assert read_csv(tmp_path / "c" / "pool_blocks.csv")[0]["reason"] == f"'{lead}cmd"

    run_export(db, tmp_path / "j", fmt="jsonl")
    assert read_jsonl(tmp_path / "j" / "jobs.jsonl")[0]["reason"] == f"{lead}HYPERLINK(1)"


def test_export_csv_keeps_plain_text(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store, jobs = open_db(home)
    try:
        job = make_job("j_plain")
        job.reason = "chosen r-go = cheapest"
        jobs.create(job)
    finally:
        store.close()

    run_export(home / "agent-router.sqlite", tmp_path / "c", fmt="csv")
    assert read_csv(tmp_path / "c" / "jobs.csv")[0]["reason"] == "chosen r-go = cheapest"


def test_export_effort_end_to_end(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store, jobs = open_db(home)
    try:
        jobs.create(make_job("j_effort"))
        jobs.add_attempt(make_attempt("j_effort", 1, model="opencode-go/glm-5.3#high"))
        jobs.add_attempt(make_attempt("j_effort", 2, route="r-old"))  # old attempt: no snapshot
        jobs.add_attempt(make_attempt("j_effort", 3, route="r-gone"))  # route not in config
    finally:
        store.close()
    db = home / "agent-router.sqlite"
    route_models = {"r-old": "kimi-code/k3.1-pro#low", "r-go": "opencode-go/glm-5.3"}

    run_export(db, tmp_path / "j", route_models=route_models)
    [job] = read_jsonl(tmp_path / "j" / "jobs.jsonl")
    assert [a["effort"] for a in job["attempts"]] == ["high", "low", None]

    run_export(db, tmp_path / "c", fmt="csv", route_models=route_models)
    rows = read_csv(tmp_path / "c" / "jobs.csv")
    assert [row["attempt_effort"] for row in rows] == ["high", "low", ""]


def test_export_works_without_server(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = fill(home)  # database closed: gateway not running
    paths = run_export(db, tmp_path / "closed")
    assert paths
    # and next to a running gateway (WAL): export neither waits nor blocks it
    store, jobs = open_db(home)
    try:
        assert run_export(db, tmp_path / "open")
        jobs.create(make_job("j_later", created_at=T0 + timedelta(hours=2)))
    finally:
        store.close()


def test_export_db_path_with_space_and_hash(tmp_path: Path) -> None:
    db = fill(tmp_path / "дом шлюза #1")  # non-ascii on purpose
    out = tmp_path / "выгрузка #1"  # non-ascii on purpose
    run_export(db, out)
    [job] = read_jsonl(out / "jobs.jsonl")
    assert job["id"] == "j_secret"


def test_export_missing_db_does_not_create_it(tmp_path: Path) -> None:
    db = tmp_path / "нет" / "agent-router.sqlite"  # non-ascii on purpose
    with pytest.raises(FileNotFoundError):
        run_export(db, tmp_path / "out")
    assert not db.exists()


def old_schema_db(home: Path) -> Path:
    """Database at schema 5: a gateway from an older version hasn't migrated it to settings,
    decision, and ratings yet."""
    store = SqliteStore(home / "agent-router.sqlite")
    try:
        for version in range(2, 6):
            store.migrate(version, [])
    finally:
        store.close()
    return home / "agent-router.sqlite"


def test_export_old_schema_is_clear_error_and_untouched(tmp_path: Path) -> None:
    db = old_schema_db(tmp_path / "home")
    with pytest.raises(ExportError, match="database schema is version 5"):
        run_export(db, tmp_path / "out")
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (5,)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "settings" not in tables
    assert not (tmp_path / "out").exists()


# --- CLI command -------------------------------------------------------------------


@pytest.fixture
def cli_home(config, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    return home


def test_cli_export_writes_files(cli_home: Path, tmp_path: Path) -> None:
    fill(cli_home)
    out = tmp_path / "cli out"
    result = CliRunner().invoke(
        app,
        ["export", "--since", "2026-11-12", "--until", "2026-11-14", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "jobs.jsonl" in result.output
    [job] = read_jsonl(out / "jobs.jsonl")
    assert job["id"] == "j_secret"


def test_cli_export_csv_default_dir(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill(cli_home)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(app, ["export", "--since", "2026-11-12", "--format", "csv"])
    assert result.exit_code == 0, result.output
    [out] = list(work.iterdir())
    assert out.name.startswith("agent-router-export-")
    assert (out / "jobs.csv").is_file()


def test_cli_export_rejects_bad_date_and_format(cli_home: Path) -> None:
    fill(cli_home)
    runner = CliRunner()
    result = runner.invoke(app, ["export", "--since", "вчера"])  # non-ascii on purpose
    assert result.exit_code == 1
    result = runner.invoke(app, ["export", "--since", "2026-11-12", "--format", "xml"])
    assert result.exit_code != 0


def test_cli_export_old_schema(cli_home: Path, tmp_path: Path) -> None:
    old_schema_db(cli_home)
    result = CliRunner().invoke(app, ["export", "--since", "2026-11-12", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "database schema is version 5" in result.output


def test_cli_export_without_db(cli_home: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["export", "--since", "2026-11-12", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert not (cli_home / "agent-router.sqlite").exists()
