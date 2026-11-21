"""One serve process per gateway home: the OS lock on serve.lock."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from agent_router import cli
from agent_router.lifecycle import InstanceLocked, instance_lock


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


def test_second_lock_fails(home: Path) -> None:
    """Two entries in one process through different file handles."""
    home.mkdir(parents=True)
    with instance_lock(home), pytest.raises(InstanceLocked) as exc, instance_lock(home):
        pass
    assert exc.value.home == home
    assert str(exc.value) == f"gateway already running for {home}"


def test_lock_released_on_exit(home: Path) -> None:
    home.mkdir(parents=True)
    with instance_lock(home):
        pass

    with instance_lock(home):
        pass


def test_stale_lock_file_does_not_block(home: Path) -> None:
    """A serve.lock left behind by a dead process has no holder and must not block."""
    home.mkdir(parents=True)
    (home / "serve.lock").write_bytes(b"")

    with instance_lock(home):
        pass


def test_serve_exits_when_locked(
    home: Path, config, tokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(True))
    runner = CliRunner()

    with instance_lock(home):
        result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 1
    assert f"gateway already running for {home}" in result.output
    assert started == []


def test_lock_io_error_is_not_reported_as_running(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only lock contention means another gateway; other errors go up unchanged."""
    import errno

    from agent_router import lifecycle

    def broken(*args: object) -> None:
        raise OSError(errno.EIO, "disk error")

    home.mkdir(parents=True)
    if sys.platform == "win32":
        monkeypatch.setattr(lifecycle.msvcrt, "locking", broken)
    else:
        monkeypatch.setattr(lifecycle.fcntl, "flock", broken)
    with pytest.raises(OSError) as exc, instance_lock(home):
        pass
    assert not isinstance(exc.value, InstanceLocked)
    assert exc.value.errno == errno.EIO


def test_serve_reports_lock_io_error(
    home: Path, config, tokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def failing(home: Path):
        raise PermissionError(13, "read-only")
        yield

    started: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(True))
    monkeypatch.setattr(cli, "instance_lock", failing)

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 1
    assert "failed to lock gateway home directory" in result.output
    assert started == []
