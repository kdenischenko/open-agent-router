"""Activity signals of a silent harness: CPU time and file changes count as signs of life."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from agent_router import activity
from agent_router.activity import (
    CPU_SHARE,
    SKIP_DIRS,
    ActivitySampler,
    newest_mtime,
)


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_cpu_growth_above_the_threshold_is_active() -> None:
    cpu = 0.0

    def read_cpu() -> float:
        return cpu

    sampler = ActivitySampler(Path("."), cpu_reader=read_cpu, mtime_reader=lambda: 0.0)
    assert sampler.sample(0.0) is None  # the first sample only sets the baseline
    cpu = 10.0
    assert sampler.sample(30.0) == "cpu"


def test_cpu_growth_below_the_threshold_is_not_active() -> None:
    cpu = 0.0

    def read_cpu() -> float:
        return cpu

    sampler = ActivitySampler(Path("."), cpu_reader=read_cpu, mtime_reader=lambda: 0.0)
    sampler.sample(0.0)
    cpu = CPU_SHARE * 30.0 / 2
    assert sampler.sample(30.0) is None


def test_cpu_growth_at_the_threshold_is_active() -> None:
    cpu = 0.0

    def read_cpu() -> float:
        return cpu

    sampler = ActivitySampler(Path("."), cpu_reader=read_cpu, mtime_reader=lambda: 0.0)
    sampler.sample(0.0)
    cpu = CPU_SHARE * 30.0
    assert sampler.sample(30.0) == "cpu"


def test_background_noise_below_one_percent_of_a_core_is_not_active() -> None:
    # an idle runtime with timers spends a few milliseconds a second: that is not work
    cpu = 0.0

    def read_cpu() -> float:
        return cpu

    sampler = ActivitySampler(Path("."), cpu_reader=read_cpu, mtime_reader=lambda: 0.0)
    sampler.sample(0.0)
    cpu = 0.1
    assert sampler.sample(30.0) is None


def test_cpu_threshold_follows_the_time_between_samples() -> None:
    cpu = 0.0

    def read_cpu() -> float:
        return cpu

    sampler = ActivitySampler(Path("."), cpu_reader=read_cpu, mtime_reader=lambda: 0.0)
    sampler.sample(0.0)
    cpu = 0.05  # half a core over a tenth of a second
    assert sampler.sample(0.1) == "cpu"


def test_a_failing_cpu_reader_gives_no_signal() -> None:
    def broken() -> float:
        raise OSError("accounting not available")

    sampler = ActivitySampler(Path("."), cpu_reader=broken, mtime_reader=lambda: 0.0)
    assert sampler.sample(0.0) is None
    assert sampler.sample(30.0) is None


def test_a_missing_cpu_reader_gives_no_signal() -> None:
    sampler = ActivitySampler(Path("."), cpu_reader=None, mtime_reader=lambda: 0.0)
    assert sampler.sample(0.0) is None
    assert sampler.sample(30.0) is None


def test_newer_mtime_is_active() -> None:
    stamps = iter([100.0, 200.0])
    sampler = ActivitySampler(
        Path("."),
        cpu_reader=None,
        mtime_reader=lambda: next(stamps),
    )
    assert sampler.sample(0.0) is None
    assert sampler.sample(30.0) == "files"


def test_the_same_mtime_is_not_active() -> None:
    sampler = ActivitySampler(
        Path("."),
        cpu_reader=None,
        mtime_reader=lambda: 100.0,
    )
    sampler.sample(0.0)
    assert sampler.sample(30.0) is None


def test_a_failing_mtime_reader_gives_no_signal() -> None:
    def broken() -> float:
        raise OSError("directory not readable")

    sampler = ActivitySampler(Path("."), cpu_reader=None, mtime_reader=broken)
    assert sampler.sample(0.0) is None
    assert sampler.sample(30.0) is None


def test_newest_mtime_skips_the_ignored_directories(tmp_path: Path) -> None:
    _write(tmp_path / "file.txt")
    _write(tmp_path / ".git" / "config")
    _write(tmp_path / ".venv" / "pyvenv.cfg")
    _write(tmp_path / "node_modules" / "index.js")
    _write(tmp_path / "__pycache__" / "mod.pyc")
    old = 1000.0
    os.utime(tmp_path / "file.txt", (old, old))
    for name in SKIP_DIRS:
        stamp = old + 500
        os.utime(tmp_path / name, (stamp, stamp))
        os.utime(tmp_path / name / list((tmp_path / name).iterdir())[0], (stamp, stamp))

    assert newest_mtime(tmp_path, None) == old


def test_newest_mtime_follows_the_newest_file(tmp_path: Path) -> None:
    first = _write(tmp_path / "first.txt")
    second = _write(tmp_path / "second.txt")
    os.utime(first, (100.0, 100.0))
    os.utime(second, (200.0, 200.0))

    assert newest_mtime(tmp_path, None) == 200.0


def test_newest_mtime_skips_the_attempt_log_directory(tmp_path: Path) -> None:
    logs = tmp_path / "logs" / "attempt-1"
    _write(logs / "stdout.log")
    source = _write(tmp_path / "file.txt")
    os.utime(logs / "stdout.log", (900.0, 900.0))
    os.utime(source, (100.0, 100.0))

    assert newest_mtime(tmp_path, logs) == 100.0


def test_newest_mtime_respects_the_file_cap(tmp_path: Path) -> None:
    for index in range(10):
        _write(tmp_path / f"file-{index}.txt")
    os.utime(tmp_path / "file-9.txt", (900.0, 900.0))

    seen = newest_mtime(tmp_path, None, cap=2)

    assert seen is not None


def test_newest_mtime_of_an_empty_directory_is_none(tmp_path: Path) -> None:
    assert newest_mtime(tmp_path, None) is None


def test_newest_mtime_of_a_missing_directory_is_none(tmp_path: Path) -> None:
    assert newest_mtime(tmp_path / "missing", None) is None


def test_newest_mtime_ignores_unreadable_entries(tmp_path: Path) -> None:
    source = _write(tmp_path / "file.txt")
    os.utime(source, (100.0, 100.0))
    broken = tmp_path / "broken"
    broken.mkdir()
    os.utime(broken, (900.0, 900.0))

    assert newest_mtime(tmp_path, None) == 100.0


@pytest.mark.parametrize("signal", ["cpu", "files"])
def test_a_sample_reports_the_signal_it_found(tmp_path: Path, signal: str) -> None:
    cpu = 0.0
    mtime = 0.0

    def read_cpu() -> float:
        return cpu

    def read_mtime() -> float:
        return mtime

    sampler = ActivitySampler(tmp_path, cpu_reader=read_cpu, mtime_reader=read_mtime)
    assert sampler.sample(0.0) is None
    if signal == "cpu":
        cpu = 5.0
    else:
        mtime = 5.0
    assert sampler.sample(30.0) == signal


def test_proc_stat_cpu_time_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The utime and stime fields of /proc/<pid>/stat are the ones that count."""
    stat = "42 (python3) S 1 42 42 0 -1 4194304 100 0 0 0 300 120 0 0 20 0 3 0 1000\n"
    monkeypatch.setattr(activity.sys, "platform", "linux")
    monkeypatch.setattr(activity.os, "getpgid", lambda _pid: 42, raising=False)
    monkeypatch.setattr(activity.os, "listdir", lambda _path: ["42", "not-a-pid"])
    monkeypatch.setattr(activity.os, "sysconf", lambda _name: 100, raising=False)

    def read_stat(path: str, **kwargs):
        assert path == "/proc/42/stat"
        return io.StringIO(stat)

    monkeypatch.setattr("builtins.open", read_stat)
    # 300 + 120 ticks at 100 ticks per second
    assert activity.posix_cpu_time_s(42) == pytest.approx(4.2)


def test_proc_stat_of_another_group_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stat = "42 (python3) S 1 42 42 0 -1 4194304 100 0 0 0 300 120 0 0 20 0 3 0 1000\n"
    monkeypatch.setattr(activity.sys, "platform", "linux")
    monkeypatch.setattr(activity.os, "getpgid", lambda _pid: 7, raising=False)
    monkeypatch.setattr(activity.os, "listdir", lambda _path: ["42"])
    monkeypatch.setattr(activity.os, "sysconf", lambda _name: 100, raising=False)
    monkeypatch.setattr("builtins.open", lambda path, **kwargs: io.StringIO(stat))

    assert activity.posix_cpu_time_s(42) is None


def test_proc_cpu_time_is_none_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity.sys, "platform", "linux")

    def no_proc(path: str):
        raise OSError("no /proc here")

    monkeypatch.setattr(activity.os, "listdir", no_proc)
    assert activity.posix_cpu_time_s(42) is None
