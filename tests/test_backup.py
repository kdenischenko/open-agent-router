"""`agent-router backup`: a consistent copy of the store taken while the gateway writes."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router import cli
from agent_router.backup import backup_database
from agent_router.clock import FakeClock
from agent_router.config import load_config
from agent_router.jobstore import open_jobstore
from agent_router.store import open_store
from test_purge import make_job

NOW = datetime(2026, 11, 27, 9, 5, 7, tzinfo=UTC)
DEFAULT_NAME = "agent-router-20261127-090507.sqlite"


def _seed_gateway(home: Path, job_ids: list[str]) -> None:
    loaded = load_config(home / "config.toml")
    store = open_store(loaded.storage, home)
    try:
        jobs = open_jobstore(loaded.storage, home, store)
        for job_id in job_ids:
            jobs.create(make_job(job_id, "completed", NOW, NOW))
    finally:
        store.close()


@pytest.fixture
def gateway_home(config, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(NOW))
    return home


def test_backup_taken_during_writes_has_every_committed_row(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    writer = sqlite3.connect(source, check_same_thread=False)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, body TEXT)")
    for number in range(500):
        writer.execute("INSERT INTO item (body) VALUES (?)", (f"row {number}",))
    writer.commit()
    assert (tmp_path / "source.sqlite-wal").stat().st_size > 0
    stop = threading.Event()

    def keep_writing() -> None:
        while not stop.is_set():
            writer.execute("INSERT INTO item (body) VALUES ('late')")
            writer.commit()

    thread = threading.Thread(target=keep_writing)
    thread.start()
    try:
        backup_database(source, tmp_path / "copy.sqlite")
    finally:
        stop.set()
        thread.join()
        writer.close()

    copy = sqlite3.connect(tmp_path / "copy.sqlite")
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        ids = [row[0] for row in copy.execute("SELECT id FROM item ORDER BY id")]
    finally:
        copy.close()
    assert len(ids) >= 500
    assert ids == list(range(1, len(ids) + 1))


def test_backup_does_not_create_a_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite"

    with pytest.raises(sqlite3.Error):
        backup_database(source, tmp_path / "copy.sqlite")

    assert not source.exists()


def test_cli_backup_writes_a_timestamped_copy_to_the_backups_directory(
    gateway_home: Path,
) -> None:
    _seed_gateway(gateway_home, ["j_000000000001", "j_000000000002"])

    result = CliRunner().invoke(cli.app, ["backup"])

    assert result.exit_code == 0, result.output
    target = gateway_home / "backups" / DEFAULT_NAME
    assert target.is_file()
    assert str(target) in result.output
    assert str(target.stat().st_size) in result.output
    copy = sqlite3.connect(target)
    try:
        count = copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        copy.close()
    assert count == 2


def test_cli_backup_writes_to_the_given_path(gateway_home: Path, tmp_path: Path) -> None:
    _seed_gateway(gateway_home, ["j_000000000001"])
    target = tmp_path / "elsewhere" / "copy.sqlite"

    result = CliRunner().invoke(cli.app, ["backup", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert not (gateway_home / "backups").exists()


def test_cli_backup_keep_removes_only_older_backups_of_the_command(gateway_home: Path) -> None:
    _seed_gateway(gateway_home, ["j_000000000001"])
    backups = gateway_home / "backups"
    backups.mkdir()
    older = [
        backups / "agent-router-20261120-010000.sqlite",
        backups / "agent-router-20261121-010000.sqlite",
        backups / "agent-router-20261122-010000.sqlite",
    ]
    for path in older:
        path.write_bytes(b"old")
    foreign = [backups / "notes.txt", backups / "agent-router-manual.sqlite"]
    for path in foreign:
        path.write_bytes(b"keep")

    result = CliRunner().invoke(cli.app, ["backup", "--keep", "2"])

    assert result.exit_code == 0, result.output
    remaining = sorted(path.name for path in backups.iterdir())
    assert remaining == sorted(
        [DEFAULT_NAME, "agent-router-20261122-010000.sqlite", *(path.name for path in foreign)]
    )


def test_cli_backup_rejects_keep_below_one(gateway_home: Path) -> None:
    _seed_gateway(gateway_home, ["j_000000000001"])

    result = CliRunner().invoke(cli.app, ["backup", "--keep", "0"])

    assert result.exit_code != 0
    assert not (gateway_home / "backups").exists()


def test_cli_backup_without_a_database_fails(gateway_home: Path) -> None:
    result = CliRunner().invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "no database" in result.output
    assert not (gateway_home / "agent-router.sqlite").exists()


def test_restore_from_backup_brings_back_the_jobs(gateway_home: Path) -> None:
    _seed_gateway(gateway_home, ["j_000000000001", "j_000000000002"])
    result = CliRunner().invoke(cli.app, ["backup"])
    assert result.exit_code == 0, result.output
    _seed_gateway(gateway_home, ["j_000000000003"])

    database = gateway_home / "agent-router.sqlite"
    database.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    (gateway_home / "backups" / DEFAULT_NAME).replace(database)

    loaded = load_config(gateway_home / "config.toml")
    store = open_store(loaded.storage, gateway_home)
    try:
        jobs = open_jobstore(loaded.storage, gateway_home, store)
        assert jobs.get("j_000000000001") is not None
        assert jobs.get("j_000000000002") is not None
        assert jobs.get("j_000000000003") is None
    finally:
        store.close()
