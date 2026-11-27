"""Retention configuration and manual purging of finished jobs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router import cli
from agent_router.clock import FakeClock
from agent_router.config import ConfigError, Pool, Window, load_config
from agent_router.jobs import Attempt, AttemptResult, Job, JobRequest, Rating, Status, Vote
from agent_router.jobstore import JobStore, SqliteJobStore, open_jobstore
from agent_router.ledger import window_state
from agent_router.periods import parse_duration
from agent_router.spend import SpendEntry
from agent_router.store import CounterSample, Observation, open_store

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


def set_tree_mtime(directory: Path, moment: datetime) -> None:
    timestamp = moment.timestamp()
    for path in (directory, *directory.rglob("*")):
        os.utime(path, (timestamp, timestamp))


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


def test_cli_purge_reports_locked_directory_and_retries_after_unlock(
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
            "j_0123456789ab", "completed", NOW - timedelta(days=41), NOW - timedelta(days=32)
        )
        removed = make_job(
            "j_removed", "completed", NOW - timedelta(days=40), NOW - timedelta(days=31)
        )
        jobs.create(blocked)
        jobs.create(removed)
    finally:
        store.close()
    blocked_dir = job_directory(home, blocked.id)
    set_tree_mtime(blocked_dir, NOW - timedelta(days=32))
    removed_dir = job_directory(home, removed.id)
    from agent_router import jobstore as jobstore_module

    remove = jobstore_module.shutil.rmtree
    locked = True

    def fail_locked(path: Path) -> None:
        nonlocal locked
        if Path(path) == blocked_dir and locked:
            locked = False
            raise OSError("locked")
        remove(path)

    monkeypatch.setattr(jobstore_module.shutil, "rmtree", fail_locked)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["purge"])

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

    retried = runner.invoke(cli.app, ["purge"])

    assert retried.exit_code == 0, retried.output
    assert (
        retried.output == "removed 1 job entry\npurged 0 observations\npurged 0 counter samples\n"
    )
    assert not blocked_dir.exists()


def test_purge_retries_after_partial_removal(
    jobstore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = make_job(
        "j_0123456789ab", "completed", NOW - timedelta(days=41), NOW - timedelta(days=32)
    )
    jobstore.create(old)
    directory = job_directory(tmp_path, old.id)
    set_tree_mtime(directory, NOW - timedelta(days=32))
    from agent_router import jobstore as jobstore_module

    remove = jobstore_module.shutil.rmtree

    def remove_one_file_then_fail(path: Path) -> None:
        (directory / "prompt.md").unlink()
        raise OSError("locked")

    monkeypatch.setattr(jobstore_module.shutil, "rmtree", remove_one_file_then_fail)

    assert jobstore.purge(CUTOFF, tmp_path / "jobs") == ([old.id], [directory])
    assert not (directory / "prompt.md").exists()
    assert (directory / "attempt-1" / "stdout.log").exists()

    monkeypatch.setattr(jobstore_module.shutil, "rmtree", remove)

    assert jobstore.purge(CUTOFF, tmp_path / "jobs") == ([old.id], [])
    assert not directory.exists()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
def test_purge_keeps_old_directory_with_nonterminal_job_row(
    jobstore, tmp_path: Path, status: Status
) -> None:
    job = make_job("j_0123456789ab", status, NOW - timedelta(days=40))
    jobstore.create(job)
    directory = job_directory(tmp_path, job.id)
    set_tree_mtime(directory, NOW - timedelta(days=40))

    assert jobstore.purge(CUTOFF, tmp_path / "jobs") == ([], [])

    assert directory.exists()
    assert jobstore.get(job.id) is not None


def test_purge_keeps_recent_orphan_and_unrelated_entries(jobstore, tmp_path: Path) -> None:
    recent = job_directory(tmp_path, "j_abcdef012345")
    set_tree_mtime(recent, NOW - timedelta(days=40))
    newest_log = recent / "attempt-1" / "stdout.log"
    recent_timestamp = (NOW - timedelta(days=1)).timestamp()
    os.utime(newest_log, (recent_timestamp, recent_timestamp))
    old = job_directory(tmp_path, "j_fedcba654321")
    set_tree_mtime(old, NOW - timedelta(days=40))
    unrelated_dir = tmp_path / "jobs" / "j_not-a-job"
    unrelated_dir.mkdir()
    (unrelated_dir / "keep.txt").write_text("keep", encoding="utf-8")
    set_tree_mtime(unrelated_dir, NOW - timedelta(days=40))
    unrelated_file = tmp_path / "jobs" / "j_000000000000"
    unrelated_file.write_text("keep", encoding="utf-8")
    old_timestamp = (NOW - timedelta(days=40)).timestamp()
    os.utime(unrelated_file, (old_timestamp, old_timestamp))

    assert jobstore.purge(CUTOFF, tmp_path / "jobs") == ([old.name], [])

    assert recent.exists() and newest_log.exists()
    assert not old.exists()
    assert unrelated_dir.exists() and (unrelated_dir / "keep.txt").exists()
    assert unrelated_file.read_text(encoding="utf-8") == "keep"


def test_purge_does_not_count_failed_orphan_removal(
    jobstore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = job_directory(tmp_path, "j_fedcba654321")
    set_tree_mtime(orphan, NOW - timedelta(days=40))
    from agent_router import jobstore as jobstore_module

    def fail_removal(path: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr(jobstore_module.shutil, "rmtree", fail_removal)

    assert jobstore.purge(CUTOFF, tmp_path / "jobs") == ([], [orphan])


def test_purge_dry_run_reports_old_orphan_without_deleting(jobstore, tmp_path: Path) -> None:
    orphan = job_directory(tmp_path, "j_fedcba654321")
    set_tree_mtime(orphan, NOW - timedelta(days=40))

    assert jobstore.purge(CUTOFF, tmp_path / "jobs", dry_run=True) == ([orphan.name], [])

    assert (orphan / "prompt.md").exists()
    assert (orphan / "attempt-1" / "stdout.log").exists()


def test_cli_purge_reports_leftover_directory_as_job_entry(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    orphan = job_directory(home, "j_0123456789ab")
    set_tree_mtime(orphan, NOW - timedelta(days=40))

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 0, result.output
    assert result.output == "removed 1 job entry\npurged 0 observations\npurged 0 counter samples\n"
    assert not orphan.exists()


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
    assert dry_run.output == (
        "would purge 1 job\nwould purge 0 observations\nwould purge 0 counter samples\n"
    )
    assert old_dir.exists()
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        assert jobs.get(old.id) is not None
    finally:
        store.close()

    purged = runner.invoke(cli.app, ["purge"])

    assert purged.exit_code == 0, purged.output
    assert purged.output == "purged 1 job\npurged 0 observations\npurged 0 counter samples\n"
    assert not old_dir.exists() and recent_dir.exists()
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        assert jobs.get(old.id) is None
        assert jobs.get(recent.id) is not None
    finally:
        store.close()


def test_purge_deletes_old_usage_rows_and_reports_counts(jobstore, store, tmp_path: Path) -> None:
    store.add_observation(Observation("p", "5h", 0.1, NOW - timedelta(days=50), None, "manual"))
    store.add_observation(Observation("p", "5h", 0.2, NOW - timedelta(days=40), None, "manual"))
    store.add_observation(Observation("p", "5h", 0.3, NOW - timedelta(days=1), None, "manual"))
    store.add_observation(Observation("q", "week", 0.4, NOW - timedelta(days=40), None, "manual"))
    store.add_counter_sample(CounterSample("p", 1.0, NOW - timedelta(days=40)))
    store.add_counter_sample(CounterSample("p", 2.0, CUTOFF))
    store.add_counter_sample(CounterSample("p", 3.0, NOW - timedelta(days=1)))
    jobs_dir = tmp_path / "jobs"

    assert jobstore.purge(CUTOFF, jobs_dir, dry_run=True, counter_samples_before=CUTOFF) == (
        [],
        [],
        2,
        1,
    )
    assert len(store.observations("p", "5h")) == 3
    assert store.last_counter("p").value_usd == 3.0

    assert jobstore.purge(CUTOFF, jobs_dir, counter_samples_before=CUTOFF) == ([], [], 2, 1)

    assert [row.fraction for row in store.observations("p", "5h")] == [0.3]
    assert store.last_observation("q", "week").fraction == 0.4
    assert [
        row.value_usd for row in store.counter_samples_since("p", NOW - timedelta(days=60))
    ] == [
        2.0,
        3.0,
    ]


def test_purge_usage_keeps_only_old_observation_for_pool_window(jobstore, store) -> None:
    old = Observation("p", "month", 0.6, NOW - timedelta(days=90), None, "manual")
    store.add_observation(old)

    assert jobstore.purge_usage(CUTOFF, CUTOFF) == (0, 0)

    assert store.last_observation("p", "month") == old


def test_purge_usage_preserves_reset_anchor_for_latest_observation(jobstore, store) -> None:
    window = Window(
        id="month",
        duration=parse_duration("1mo"),
        kind="fixed",
        role="pace",
        anchor=datetime(2026, 11, 1, tzinfo=UTC),
        limit_usd=None,
        block_at=1.0,
    )
    pool = Pool("p", "Pool", "percent", (window,), 0.0, None, None, None, None)
    reset = datetime(2026, 11, 23, tzinfo=UTC)
    store.add_observation(Observation("p", "month", 0.4, NOW - timedelta(days=40), reset, "manual"))
    store.add_observation(Observation("p", "month", 0.6, NOW - timedelta(days=1), None, "manual"))
    before = window_state(pool, window, store, NOW)
    assert (before.window_start, before.resets_at) == (
        datetime(2026, 10, 23, tzinfo=UTC),
        reset,
    )

    jobstore.purge_usage(CUTOFF, CUTOFF)

    assert window_state(pool, window, store, NOW) == before


def test_cli_purge_keeps_samples_inside_two_monthly_windows(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        """[storage]
backend = "sqlite"
retention_days = 7

[[pools]]
id = "p"
title = "Pool"
unit = "percent"

[[pools.windows]]
id = "month"
duration = "1mo"
kind = "fixed"
role = "pace"
""",
        encoding="utf-8",
    )
    config = load_config(home / "config.toml")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    store = open_store(config.storage, home)
    try:
        store.add_counter_sample(CounterSample("p", 1.0, NOW - timedelta(days=20)))
        store.add_counter_sample(CounterSample("p", 2.0, NOW - timedelta(days=70)))
    finally:
        store.close()

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 0, result.output
    assert result.output == "purged 0 jobs\npurged 0 observations\npurged 1 counter sample\n"
    store = open_store(config.storage, home)
    try:
        samples = store.counter_samples_since("p", NOW - timedelta(days=100))
        assert [(row.value_usd, row.sampled_at) for row in samples] == [
            (1.0, NOW - timedelta(days=20))
        ]
    finally:
        store.close()


def test_cli_purge_skips_usage_dry_run_before_deleting(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    calls = []
    original = SqliteJobStore.purge_usage

    def purge_usage(self, before, counter_samples_before, dry_run=False):
        calls.append(dry_run)
        return original(self, before, counter_samples_before, dry_run=dry_run)

    monkeypatch.setattr(SqliteJobStore, "purge_usage", purge_usage)

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 0, result.output
    assert calls == [False]


@pytest.mark.parametrize("args", [["purge"], ["purge", "--dry-run"]])
def test_cli_purge_with_retention_disabled_is_a_noop(
    args: list[str], config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    store = open_store(config.storage, home)
    try:
        old = Observation("p", "5h", 0.1, NOW - timedelta(days=90), None, "manual")
        store.add_observation(old)
        store.add_observation(Observation("p", "5h", 0.2, NOW - timedelta(days=80), None, "manual"))
        store.add_counter_sample(CounterSample("p", 1.0, NOW - timedelta(days=90)))
    finally:
        store.close()

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert result.output == "retention is disabled: set storage.retention_days in config.toml\n"
    store = open_store(config.storage, home)
    try:
        assert len(store.observations("p", "5h")) == 2
        assert store.last_counter("p").value_usd == 1.0
    finally:
        store.close()


def test_cli_purge_reports_observation_and_counter_sample_counts(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    store = open_store(config.storage, home)
    try:
        store.add_observation(Observation("p", "5h", 0.1, NOW - timedelta(days=50), None, "manual"))
        store.add_observation(Observation("p", "5h", 0.2, NOW - timedelta(days=40), None, "manual"))
        store.add_counter_sample(CounterSample("p", 1.0, NOW - timedelta(days=70)))
    finally:
        store.close()

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 0, result.output
    assert result.output == "purged 0 jobs\npurged 1 observation\npurged 1 counter sample\n"


def test_cli_purge_supports_jobstore_without_usage_purge(
    config, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_retention(home, "30")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    open_jobstore = cli.open_jobstore

    class JobStoreWithoutUsagePurge(JobStore):
        def __init__(self, jobs) -> None:
            self.jobs = jobs

        def get(self, job_id: str):
            return self.jobs.get(job_id)

        def purge(self, before: datetime, jobs_dir: Path, dry_run: bool = False):
            return self.jobs.purge(before, jobs_dir, dry_run=dry_run)

    def open_without_usage_purge(settings, home, store):
        return JobStoreWithoutUsagePurge(open_jobstore(settings, home, store))

    monkeypatch.setattr(cli, "open_jobstore", open_without_usage_purge)

    result = CliRunner().invoke(cli.app, ["purge"])

    assert result.exit_code == 0, result.output
    assert result.output == "purged 0 jobs\npurged 0 observations\npurged 0 counter samples\n"
