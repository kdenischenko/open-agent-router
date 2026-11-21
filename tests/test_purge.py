"""Retention configuration and manual purging of finished jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router import cli
from agent_router.clock import FakeClock
from agent_router.config import ConfigError, load_config
from agent_router.jobs import Attempt, AttemptResult, Job, JobRequest, Rating, Status, Vote
from agent_router.jobstore import open_jobstore
from agent_router.spend import SpendEntry
from agent_router.store import open_store

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)


def make_job(
    job_id: str,
    status: Status,
    created_at: datetime,
    finished_at: datetime | None = None,
) -> Job:
    return Job(
        id=job_id,
        request=JobRequest(f"prompt for {job_id}", Path("/work/project")),
        status=status,
        tier="standard",
        tier_source="default",
        repo_root=None,
        created_at=created_at,
        finished_at=finished_at,
        text=f"result for {job_id}" if finished_at is not None else None,
    )


def job_directory(home: Path, job_id: str) -> Path:
    directory = home / "jobs" / job_id
    attempt = directory / "attempt-1"
    attempt.mkdir(parents=True)
    (directory / "prompt.md").write_text(f"prompt for {job_id}", encoding="utf-8")
    (attempt / "stdout.log").write_text(f"result for {job_id}", encoding="utf-8")
    (attempt / "stderr.log").write_text("", encoding="utf-8")
    return directory


def set_retention(home: Path, value: str) -> None:
    path = home / "config.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('backend = "sqlite"', f'backend = "sqlite"\nretention_days = {value}'),
        encoding="utf-8",
    )


def test_retention_defaults_to_keep_forever(config) -> None:
    assert config.storage.retention_days is None


def test_retention_days_is_loaded_outside_backend_options(config, home: Path) -> None:
    set_retention(home, "30")

    loaded = load_config(home / "config.toml")

    assert loaded.storage.retention_days == 30
    assert "retention_days" not in loaded.storage.options


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true", '"30"'])
def test_retention_days_must_be_a_positive_integer(config, home: Path, value: str) -> None:
    set_retention(home, value)

    with pytest.raises(ConfigError) as caught:
        load_config(home / "config.toml")

    assert any("storage.retention_days" in message for message in caught.value.messages)


def test_purge_dry_run_then_deletes_old_terminal_jobs_and_related_data(
    jobstore, store, tmp_path: Path
) -> None:
    old = make_job("j_old", "completed", NOW - timedelta(days=40), NOW - timedelta(days=31))
    old_without_finish = make_job("j_old_without_finish", "failed", NOW - timedelta(days=41))
    kept = [
        make_job("j_boundary", "completed", NOW - timedelta(days=40), CUTOFF),
        make_job("j_recent", "completed", NOW - timedelta(days=40), NOW - timedelta(days=1)),
        make_job("j_queued", "queued", NOW - timedelta(days=40)),
        make_job("j_running", "running", NOW - timedelta(days=40)),
        make_job("j_waiting", "awaiting_approval", NOW - timedelta(days=40)),
    ]
    for job in (old, old_without_finish, *kept):
        jobstore.create(job)
    jobstore.add_attempt(
        Attempt(
            job_id=old.id,
            n=1,
            route="route",
            pool="pool",
            started_at=old.created_at,
            finished_at=old.finished_at,
            outcome="ok",
            log_tail="old attempt log",
            result=AttemptResult("ok", text="old result"),
        )
    )
    jobstore.add_rating(Rating(old.id, "good", None, None, None, None, NOW))
    jobstore.add_vote(old.id, Vote("classifier", "model", "standard", 0.9, 10, None), NOW)
    jobstore.add_spend(SpendEntry("pool", NOW, "job", "route", 1, 1, None, False, None, old.id, 1))
    jobstore.add_spend(
        SpendEntry("pool", NOW, "proxy", "route", 1, 1, None, False, None, old.id, 1)
    )
    jobs_dir = tmp_path / "jobs"
    old_dir = job_directory(tmp_path, old.id)
    old_without_finish_dir = job_directory(tmp_path, old_without_finish.id)
    kept_dirs = {job.id: job_directory(tmp_path, job.id) for job in kept}

    window_start = NOW - timedelta(days=7)
    spend_before = jobstore.spend_since("pool", window_start)
    assert jobstore.purge(CUTOFF, jobs_dir, dry_run=True) == (
        [old_without_finish.id, old.id],
        [],
    )
    assert jobstore.get(old.id) is not None
    assert old_dir.exists() and old_without_finish_dir.exists()

    assert jobstore.purge(CUTOFF, jobs_dir) == ([old_without_finish.id, old.id], [])

    assert jobstore.get(old.id) is None
    assert jobstore.get(old_without_finish.id) is None
    assert jobstore.attempts(old.id) == []
    assert jobstore.last_rating(old.id) is None
    assert jobstore.votes(old.id) == []
    assert not old_dir.exists() and not old_without_finish_dir.exists()
    for job in kept:
        assert jobstore.get(job.id) is not None
        assert kept_dirs[job.id].exists()
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == len(kept)
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM classifier_votes").fetchone()[0] == 0
    assert jobstore.spend_since("pool", window_start) == spend_before
    assert spend_before.jobs == 1
    assert spend_before.requests == 1


def test_cli_purge_reports_locked_directory_and_continues(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    loaded = load_config(home / "config.toml")
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        blocked = make_job(
            "j_blocked", "completed", NOW - timedelta(days=41), NOW - timedelta(days=32)
        )
        removed = make_job(
            "j_removed", "completed", NOW - timedelta(days=40), NOW - timedelta(days=31)
        )
        jobs.create(blocked)
        jobs.create(removed)
    finally:
        store.close()
    blocked_dir = job_directory(home, blocked.id)
    removed_dir = job_directory(home, removed.id)
    from agent_router import jobstore as jobstore_module

    remove = jobstore_module.shutil.rmtree

    def fail_locked(path: Path) -> None:
        if Path(path) == blocked_dir:
            raise OSError("locked")
        remove(path)

    monkeypatch.setattr(jobstore_module.shutil, "rmtree", fail_locked)

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 1, result.output
    assert "purged 2 jobs\n" in result.output
    assert f"warning: could not remove {blocked_dir}\n" in result.output
    assert blocked_dir.exists()
    assert not removed_dir.exists()
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        assert jobs.get(blocked.id) is None
        assert jobs.get(removed.id) is None
    finally:
        store.close()


def test_cli_purge_uses_injected_clock_and_dry_run_preserves_data(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    loaded = load_config(home / "config.toml")
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        old = make_job("j_old", "completed", NOW - timedelta(days=40), NOW - timedelta(days=31))
        recent = make_job("j_recent", "completed", NOW - timedelta(days=2), NOW - timedelta(days=1))
        jobs.create(old)
        jobs.create(recent)
    finally:
        store.close()
    old_dir = job_directory(home, old.id)
    recent_dir = job_directory(home, recent.id)
    runner = CliRunner()

    dry_run = runner.invoke(cli.app, ["purge", "--dry-run"])

    assert dry_run.exit_code == 0, dry_run.output
    assert dry_run.output == "would purge 1 job\n"
    assert old_dir.exists()
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        assert jobs.get(old.id) is not None
    finally:
        store.close()

    purged = runner.invoke(cli.app, ["purge"])

    assert purged.exit_code == 0, purged.output
    assert purged.output == "purged 1 job\n"
    assert not old_dir.exists() and recent_dir.exists()
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        assert jobs.get(old.id) is None
        assert jobs.get(recent.id) is not None
    finally:
        store.close()


def test_cli_purge_with_retention_disabled_is_a_noop(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))

    result = CliRunner().invoke(cli.app, ["purge", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.output == "retention is disabled: set storage.retention_days in config.toml\n"
    assert not (home / "agent-router.sqlite").exists()
