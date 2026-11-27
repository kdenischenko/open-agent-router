"""Runner: launching a fake harness, timeout, idle limit, cancel, output truncation, .cmd shims."""

from __future__ import annotations

import calendar
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import types
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_router import runner as runner_module
from agent_router.adapters.opencode import OpenCodeAdapter
from agent_router.runner import (
    Invocation,
    JobQueryError,
    LocalRunner,
    ProcessQueryError,
    ShimUnsafeArgs,
    check_shim_args,
    kill_tree,
    process_image,
    resolve_executable,
)
from helpers import pid_alive

HARNESS = str(Path(__file__).parent / "stubs" / "fake_harness.py")


def harness(*scenario: str, stdin_text: str | None = None) -> Invocation:
    return Invocation((sys.executable, HARNESS, "--scenario", *scenario), stdin_text=stdin_text)


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


def wait_dead(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "каталог с пробелом"  # non-ascii on purpose
    path.mkdir()
    return path


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    return tmp_path / "журнал задачи"  # non-ascii on purpose


def run(inv: Invocation, work: Path, logs: Path, timeout_s: float = 60, **kwargs):
    cancel = kwargs.pop("cancel", threading.Event())
    return LocalRunner().run(inv, work, timeout_s, logs, cancel, **kwargs)


def test_success_writes_stdout_and_stderr_files(work: Path, logs: Path) -> None:
    source = work / "ответ.txt"  # non-ascii on purpose
    source.write_text("привет из файла\n", encoding="utf-8")  # non-ascii on purpose
    result = run(harness("echo-file", str(source)), work, logs)
    assert result.exit_code == 0
    assert not (result.timed_out or result.cancelled or result.truncated)
    assert not result.forced_after_final
    assert result.stdout_path == logs / "stdout.log" and result.stderr_path == logs / "stderr.log"
    assert read(result.stdout_path) == "привет из файла\n"  # non-ascii on purpose
    assert "stderr line" in read(result.stderr_path)
    assert result.duration_s > 0


def test_nonzero_exit_code(work: Path, logs: Path) -> None:
    result = run(harness("exit", "7"), work, logs)
    assert result.exit_code == 7
    assert not result.timed_out and not result.cancelled


def test_timeout_kills_child_process_too(work: Path, logs: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    result = run(harness("child-sleep", "120", str(pid_file)), work, logs, timeout_s=1)
    assert result.timed_out and not result.cancelled
    assert not result.idle
    child = int(pid_file.read_text(encoding="utf-8"))
    assert wait_dead(child, 5)


def test_silent_harness_is_stopped_as_idle(work: Path, logs: Path) -> None:
    pids: list[int] = []
    result = run(
        harness("sleep", "120"),
        work,
        logs,
        timeout_s=60,
        idle_timeout_s=1,
        on_start=pids.append,
    )
    assert result.timed_out and result.idle and not result.cancelled
    assert result.duration_s < 10  # the hard cap of 60 s was never reached
    assert pids and wait_dead(pids[0], 5)


def test_ticking_harness_outlives_idle_limit(work: Path, logs: Path) -> None:
    result = run(harness("tick", "8", "0.3"), work, logs, timeout_s=60, idle_timeout_s=1)
    assert result.exit_code == 0
    assert not result.timed_out and not result.idle
    assert read(result.stdout_path).splitlines()[-1] == "ok:done"


def test_opencode_retries_do_not_reset_idle_limit(work: Path, logs: Path) -> None:
    result = run(
        harness("retry", "20", "0.3"),
        work,
        logs,
        timeout_s=3,
        idle_timeout_s=1,
        is_progress_line=OpenCodeAdapter().is_progress_line,
    )
    assert result.timed_out and result.idle and not result.cancelled
    lines = read(result.stdout_path).splitlines()
    assert lines and all(json.loads(line)["type"] == "retry" for line in lines)


def test_opencode_progress_lines_reset_idle_limit(work: Path, logs: Path) -> None:
    result = run(
        harness("tick", "8", "0.3"),
        work,
        logs,
        timeout_s=60,
        idle_timeout_s=1,
        is_progress_line=OpenCodeAdapter().is_progress_line,
    )
    assert result.exit_code == 0
    assert not result.timed_out and not result.idle
    assert read(result.stdout_path).splitlines()[-1] == "ok:done"


def test_stderr_ticks_count_as_progress(work: Path, logs: Path) -> None:
    result = run(harness("tick", "8", "0.3", "stderr"), work, logs, timeout_s=60, idle_timeout_s=1)
    assert result.exit_code == 0
    assert not result.timed_out and not result.idle
    assert "tick 8" in read(result.stderr_path)


def test_silent_harness_with_file_changes_is_not_idle_when_quiet(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that works without printing keeps the attempt alive when the adapter says so."""
    monkeypatch.setattr(runner_module, "ACTIVITY_SAMPLE_S", 0.1)
    result = run(
        harness("work-silently", str(work / "work.txt"), "1.2"),
        work,
        logs,
        timeout_s=30,
        idle_timeout_s=1,
        quiet_while_working=True,
    )
    assert not result.idle
    assert not result.timed_out
    assert result.exit_code == 0
    assert (work / "work.txt").exists()  # the harness kept working in its directory


def test_silent_harness_with_file_changes_is_idle_when_not_quiet(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "ACTIVITY_SAMPLE_S", 0.1)
    result = run(
        harness("work-silently", str(work / "work.txt"), "5"),
        work,
        logs,
        timeout_s=30,
        idle_timeout_s=1,
        quiet_while_working=False,
    )
    assert result.idle and result.timed_out
    assert result.duration_s < 10  # the hard cap of 30 s was never reached


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="the CPU signal is read on Windows and Linux"
)
def test_quiet_harness_reports_the_last_activity_signal(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that burns CPU without printing is not stopped as idle."""
    monkeypatch.setattr(runner_module, "ACTIVITY_SAMPLE_S", 0.1)
    result = run(
        harness("quiet-busy", "2"),
        work,
        logs,
        timeout_s=30,
        idle_timeout_s=1,
        quiet_while_working=True,
    )
    assert not result.idle
    assert result.exit_code == 0
    assert result.last_signal == "cpu"
    assert result.last_signal_age_s is not None


def test_quiet_harness_without_signals_has_no_signal(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "ACTIVITY_SAMPLE_S", 0.1)
    result = run(
        harness("quiet-sleep", "5"),
        work,
        logs,
        timeout_s=30,
        idle_timeout_s=1,
    )
    assert result.idle
    assert result.last_signal is None
    assert result.last_signal_age_s is None


def test_job_cpu_time_counts_as_activity(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CPU time of the job object is a sign of life for a silent harness."""
    monkeypatch.setattr(runner_module, "ACTIVITY_SAMPLE_S", 0.1)
    job = with_job(monkeypatch)
    readings = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    job.cpu_time_s_value = None
    monkeypatch.setattr(job, "cpu_time_s", lambda: next(readings, 10.0))
    result = run(
        harness("quiet-sleep", "5"),
        work,
        logs,
        timeout_s=30,
        idle_timeout_s=1,
        quiet_while_working=True,
    )
    assert result.idle
    assert result.last_signal == "cpu"


class _ChunkStream:
    """A readable stream that hands out the given chunks and then reports EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read1(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


def test_truncated_output_still_counts_as_progress(tmp_path: Path) -> None:
    touches: list[int] = []

    def activity() -> None:
        touches.append(1)

    pump = runner_module._Pump(
        _ChunkStream([b"one\n", b"two\n", b"three\n", b"four\n"]),
        tmp_path / "stdout.log",
        10,
        on_activity=activity,
    )
    pump.start()
    pump.join(5)
    assert not pump.is_alive()
    assert len(touches) == 4  # every chunk, also the one read after the output was truncated
    assert pump.truncated is True


def _pump_activity(tmp_path: Path, chunks: list[bytes], monkeypatch: pytest.MonkeyPatch) -> int:
    """Run a pump with the OpenCode progress predicate and count activity calls."""
    monkeypatch.setattr(runner_module, "_LINE_BUFFER_MAX", 64)
    touches: list[int] = []
    pump = runner_module._Pump(
        _ChunkStream(chunks),
        tmp_path / "stdout.log",
        1 << 20,
        on_activity=lambda: touches.append(1),
        is_progress_line=OpenCodeAdapter().is_progress_line,
    )
    pump.start()
    pump.join(5)
    assert not pump.is_alive()
    return len(touches)


def test_unterminated_plain_output_counts_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _pump_activity(tmp_path, [b"working", b"..."], monkeypatch) >= 2


def test_split_retry_event_is_not_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [b'{"type":"ret', b'ry","attempt":1}\n']
    assert _pump_activity(tmp_path, chunks, monkeypatch) == 0


def test_suffix_of_overlong_retry_line_is_not_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = b'{"type":"retry","message":"' + b"x" * 100
    chunks = [head, b"y" * 10, b'"}\n', b'{"type":"retry"}\n']
    assert _pump_activity(tmp_path, chunks, monkeypatch) == 0


def test_line_after_overlong_line_is_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = b'{"type":"retry","message":"' + b"x" * 100
    chunks = [head, b'"}\n{"type":"text"}\n']
    assert _pump_activity(tmp_path, chunks, monkeypatch) == 1


def test_final_line_hang_is_forced_not_idle(work: Path, logs: Path) -> None:
    result = run(
        harness("final-then-hang", "FINAL-LINE"),
        work,
        logs,
        timeout_s=60,
        is_final_line=lambda line: line == "FINAL-LINE",
        exit_grace_s=1,
        idle_timeout_s=0.5,
    )
    assert result.forced_after_final
    assert not result.timed_out and not result.idle
    assert result.exit_code == 0


def test_cancel_by_event(work: Path, logs: Path) -> None:
    cancel = threading.Event()
    started = threading.Event()
    pids: list[int] = []

    def on_start(pid: int) -> None:
        pids.append(pid)
        started.set()

    def cancel_later() -> None:
        started.wait(30)
        cancel.set()

    helper = threading.Thread(target=cancel_later)
    helper.start()
    try:
        result = run(harness("sleep", "120"), work, logs, cancel=cancel, on_start=on_start)
    finally:
        helper.join()
    assert result.cancelled and not result.timed_out
    assert wait_dead(pids[0], 5)


def test_big_output_is_truncated(work: Path, logs: Path) -> None:
    result = run(harness("big", "25"), work, logs)
    assert result.truncated
    assert result.exit_code == 0  # pipe drained to the end, process did not stall on write
    size = result.stdout_path.stat().st_size
    assert LocalRunner.max_stream_bytes <= size < LocalRunner.max_stream_bytes + 1000
    assert "output truncated" in read(result.stdout_path)[-200:]


def test_stdin_text_is_delivered_and_stream_closed(work: Path, logs: Path) -> None:
    text = "--auto\nпромпт «в кавычках» & %PATH%\n"  # non-ascii on purpose
    result = run(harness("stdin-echo", stdin_text=text), work, logs)
    assert result.exit_code == 0  # scenario reads the stream to the end: stream was closed
    assert read(result.stdout_path) == text


def test_stdin_is_closed_without_text(work: Path, logs: Path) -> None:
    result = run(harness("wait-stdin-eof"), work, logs, timeout_s=30)
    assert result.exit_code == 0 and not result.timed_out
    assert read(result.stdout_path) == "eof\n"


def test_final_line_then_hang_is_forced_success(work: Path, logs: Path) -> None:
    result = run(
        harness("final-then-hang", "FINAL-LINE"),
        work,
        logs,
        timeout_s=60,
        is_final_line=lambda line: line == "FINAL-LINE",
        exit_grace_s=1,
    )
    assert result.forced_after_final
    assert result.exit_code == 0
    assert not result.timed_out and not result.cancelled
    assert "FINAL-LINE" in read(result.stdout_path)


def test_environment_has_no_color(work: Path, logs: Path) -> None:
    result = run(harness("env", "NO_COLOR"), work, logs)
    assert read(result.stdout_path).strip() == "1"


def test_pwd_points_to_task_dir(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`opencode run` takes the project directory from PWD, not from the process cwd:
    an inherited gateway PWD used to misdirect the record into the gateway's launch dir."""
    monkeypatch.setenv("PWD", str(tmp_path / "elsewhere"))
    result = run(harness("env", "PWD"), work, logs)
    assert read(result.stdout_path).strip() == str(work)


def test_tricky_argument_arrives_verbatim(work: Path, logs: Path) -> None:
    tricky = '--auto & echo "x" %PATH%\nвторая строка'  # non-ascii on purpose
    result = run(harness("argv-echo", tricky), work, logs)
    assert json.loads(read(result.stdout_path)) == [tricky]


def test_on_start_gets_pid_of_live_process(work: Path, logs: Path) -> None:
    seen: list[tuple[int, bool]] = []
    run(
        harness("sleep", "1"),
        work,
        logs,
        on_start=lambda pid: seen.append((pid, pid_alive(pid))),
    )
    assert len(seen) == 1 and seen[0][0] > 0 and seen[0][1] is True


# --- .cmd / .bat wrappers ---------------------------------------------------------------


@pytest.mark.parametrize("char", list('&|^%"!<>()') + ["\n"])
def test_shim_rejects_unsafe_argument(char: str, work: Path, logs: Path, tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    shim = tmp_path / "tool.cmd"
    shim.write_text(f"@echo started > {marker.name}\r\n", encoding="ascii")
    inv = Invocation((str(shim), "run", f"до{char}после"))  # non-ascii on purpose
    with pytest.raises(ShimUnsafeArgs):
        check_shim_args(inv.argv)
    with pytest.raises(ShimUnsafeArgs):
        run(inv, tmp_path, logs)
    assert not marker.exists()  # process was not started
    assert not logs.exists() or not any(logs.iterdir())


def test_shim_safe_arguments_and_plain_executable() -> None:
    cyrillic_path = "каталог с пробелом/файл.md"  # non-ascii on purpose
    check_shim_args(("tool.cmd", "run", "--model", "prov/model-1.5", cyrillic_path))
    check_shim_args(("tool.BAT", "a=b", "x@y", "c:\\dir\\file_1.txt", "+1", "-k"))
    check_shim_args(("tool.exe", '& | ^ % " ! < > ( )\n'))  # not a wrapper: no check needed
    check_shim_args(())


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd shims are Windows-specific")
def test_cmd_shim_passes_argument_with_space_verbatim(
    work: Path, logs: Path, tmp_path: Path
) -> None:
    shim = tmp_path / "shim.cmd"
    shim.write_text(
        f'@"{sys.executable}" "{HARNESS}" --scenario argv-echo %*\r\n', encoding="ascii"
    )
    argument = "каталог с пробелом/файл.md"  # non-ascii on purpose
    result = run(Invocation((str(shim), argument)), work, logs)
    assert result.exit_code == 0
    assert json.loads(read(result.stdout_path)) == [argument]


def test_resolve_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = resolve_executable(sys.executable)
    assert found.path is not None and found.path.is_file() and not found.is_shim
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    by_name = resolve_executable(Path(sys.executable).name)
    assert by_name.path is not None and by_name.path.is_file()
    missing = resolve_executable("no-such-harness-0f3a")
    assert missing.path is None and missing.is_shim is False
    shim = tmp_path / "wrapper.CMD"
    shim.write_text("@echo off\r\n", encoding="ascii")
    assert resolve_executable(str(shim)).is_shim is True


def test_tilde_in_harness_path_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tool = tmp_path / "tools" / "harness.exe"
    tool.parent.mkdir()
    tool.write_text("", encoding="ascii")

    found = resolve_executable("~/tools/harness.exe")

    assert found.path == tool.resolve()


def test_relative_executable_resolves_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = tmp_path / "tools" / "harness.cmd"
    tool.parent.mkdir()
    tool.write_text("@echo off\r\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    found = resolve_executable("tools/harness.cmd")
    assert found.path is not None and found.path.is_absolute()
    assert found.path.is_file() and found.is_shim  # a different cwd still resolves to the same file


# --- processes --------------------------------------------------------------------------


def test_process_image_names_python_for_own_process() -> None:
    image = process_image(os.getpid())
    # ps on POSIX names a script by its entry point, so the test runner may show as pytest
    assert image is not None and ("python" in image.lower() or "pytest" in image.lower())


def _missing_pid() -> int:
    if sys.platform == "win32":
        return 2**22 + 12345
    # above pid_max ps reports a syntax error, so use the pid of a child that has been reaped
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_process_image_of_missing_pid_is_none() -> None:
    assert process_image(_missing_pid()) is None


def test_posix_ps_gets_a_screen_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """procps warns about a bogus screen size on stderr when the console reports one row."""
    envs: list[dict[str, str] | None] = []

    def fake_run(argv, **kwargs):
        envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setenv("LINES", "1")

    assert process_image(4242) is None
    env = envs[0]
    assert env is not None
    assert int(env["LINES"]) > 1
    assert int(env["COLUMNS"]) >= 1000  # a long command name on macOS is not cut
    assert env.get("PATH") == os.environ.get("PATH")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_process_image_query_has_short_timeout(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    timeouts: list[float] = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(runner_module.sys, "platform", platform)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    with pytest.raises(ProcessQueryError):
        process_image(4242)
    assert timeouts == [runner_module.QUERY_WAIT_S]
    assert runner_module.QUERY_WAIT_S <= 5.0


def test_posix_missing_ps_is_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "нет такого файла", argv[0])  # non-ascii on purpose

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    with pytest.raises(ProcessQueryError):
        process_image(4242)


@pytest.mark.parametrize(
    ("platform", "code", "stdout", "stderr"),
    [
        ("win32", 2, b"", "ОШИБКА: недопустимый аргумент".encode()),  # non-ascii on purpose
        ("linux", 1, b"S    node\n", b"ps: unknown option\n"),
    ],
    ids=["tasklist-code-2", "ps-code-1-with-output"],
)
def test_process_image_error_code_is_query_error(
    monkeypatch: pytest.MonkeyPatch, platform: str, code: int, stdout: bytes, stderr: bytes
) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    monkeypatch.setattr(runner_module.sys, "platform", platform)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    with pytest.raises(ProcessQueryError):
        process_image(4242)


def test_posix_ps_error_in_stderr_is_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"ps: unrecognized option: o\n")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    with pytest.raises(ProcessQueryError):
        process_image(4242)


def test_posix_missing_pid_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    assert process_image(4242) is None


def _query_answers(outputs, calls):
    """Stub for `subprocess.run` used by `ps`: non-empty output means exit code 0,
    an empty one means code 1 (PID not found)."""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        out = next(outputs)
        return subprocess.CompletedProcess(argv, 0 if out else 1, out.encode(), b"")

    return fake_run


def test_posix_zombie_has_no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runner_module.subprocess, "run", _query_answers(iter(["Z    node\n"]), calls)
    )

    assert process_image(4242) is None
    assert calls == [(["ps", "-p", "4242", "-o", "stat=,comm="], runner_module.QUERY_WAIT_S)]


def test_posix_live_process_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runner_module.subprocess, "run", _query_answers(iter(["S    node\n", ""]), calls)
    )

    assert process_image(4242) == "node"
    assert process_image(4242) is None
    assert calls == [
        (["ps", "-p", "4242", "-o", "stat=,comm="], runner_module.QUERY_WAIT_S),
        (["ps", "-p", "4242", "-o", "stat=,comm="], runner_module.QUERY_WAIT_S),
    ]


# --- monitoring of a running process -----------------------------------------------------


def _survival_rig(
    monkeypatch: pytest.MonkeyPatch, platform: str, alive_after_kill: bool
) -> list[int]:
    """Patches the platform and process-group check; returns the `kill_tree` call log."""
    killed: list[int] = []
    monkeypatch.setattr(runner_module.sys, "platform", platform)
    monkeypatch.setattr(
        runner_module, "_group_alive", lambda pid: alive_after_kill if killed else True
    )
    monkeypatch.setattr(runner_module, "kill_tree", killed.append)
    return killed


def test_posix_live_group_after_exit_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = _survival_rig(monkeypatch, "linux", alive_after_kill=False)
    assert runner_module._survived(4242, parent_alive=False) is False
    assert killed == [4242]


def test_posix_group_surviving_kill_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = _survival_rig(monkeypatch, "linux", alive_after_kill=True)
    assert runner_module._survived(4242, parent_alive=False) is True
    assert killed == [4242]


def test_windows_dead_parent_is_not_survivor(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = _survival_rig(monkeypatch, "win32", alive_after_kill=True)
    queried: list[int] = []
    monkeypatch.setattr(runner_module, "_group_alive", lambda pid: queried.append(pid) or True)
    assert runner_module._survived(4242, parent_alive=False) is False
    assert killed == [] and queried == []


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_live_parent_is_survivor(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    killed = _survival_rig(monkeypatch, platform, alive_after_kill=True)
    assert runner_module._survived(4242, parent_alive=True) is True
    assert killed == []


def test_survivor_is_reported(work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancel = threading.Event()
    pids: list[int] = []

    def on_start(pid: int) -> None:
        pids.append(pid)
        cancel.set()

    started = time.monotonic()
    try:
        # emergency taskkill after leaving the context uses the real timeout, not the patched one
        with monkeypatch.context() as patch:
            patch.setattr(runner_module, "KILL_WAIT_S", 0.2)
            patch.setattr(runner_module, "_READER_WAIT_S", 0.2)
            patch.setattr(runner_module, "create_job", lambda: None)  # the fallback path
            patch.setattr(LocalRunner, "_kill", staticmethod(lambda proc, job=None: None))
            result = run(harness("sleep", "120"), work, logs, cancel=cancel, on_start=on_start)
        assert result.survived is True
        assert result.cancelled is True
        assert result.exit_code is None
        assert time.monotonic() - started < 5
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


@pytest.mark.parametrize("readers_hold", [True, False], ids=["held-readers", "held-stdin"])
def test_survivor_pipe_close_releases_reader_threads(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch, readers_hold: bool
) -> None:
    class HeldPipe:
        def __init__(self, holds: bool = True) -> None:
            self.closed = threading.Event()
            self.holds = holds

        def read1(self, _size: int) -> bytes:
            if not self.holds:
                return b""
            self.closed.wait()
            raise OSError("pipe closed")

        def write(self, _data: bytes) -> None:
            self.closed.wait()
            raise OSError("pipe closed")

        def close(self) -> None:
            self.closed.set()

    class HeldProcess:
        pid = 4242
        returncode = None

        def __init__(self) -> None:
            self.stdin = HeldPipe()
            self.stdout = HeldPipe(readers_hold)
            self.stderr = HeldPipe(readers_hold)

        def poll(self) -> None:
            return None

        def wait(self, timeout: float) -> None:
            raise subprocess.TimeoutExpired("fake-harness", timeout)

    proc = HeldProcess()
    cancel = threading.Event()
    pumps: list[runner_module._Pump] = []
    feeders: list[threading.Thread] = []
    real_start = runner_module._Pump.start
    real_feed = runner_module._feed

    def start_pump(pump: runner_module._Pump) -> None:
        pumps.append(pump)
        real_start(pump)

    def track_feeder(stream, text: str, stop: threading.Event) -> None:
        feeders.append(threading.current_thread())
        real_feed(stream, text, stop)

    monkeypatch.setattr(runner_module, "KILL_WAIT_S", 0.05)
    monkeypatch.setattr(runner_module, "_READER_WAIT_S", 0.05)
    monkeypatch.setattr(runner_module, "create_job", lambda: None)
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(LocalRunner, "_kill", staticmethod(lambda proc, job=None: None))
    monkeypatch.setattr(runner_module._Pump, "start", start_pump)
    monkeypatch.setattr(runner_module, "_feed", track_feeder)

    result = run(
        Invocation(("fake-harness",), stdin_text="prompt"),
        work,
        logs,
        cancel=cancel,
        on_start=lambda _pid: cancel.set(),
    )
    threads = [*pumps, *feeders]
    try:
        assert result.survived is True
        assert result.cancelled is True
        assert len(threads) == 3
        assert all(not thread.is_alive() for thread in threads)
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
        for thread in threads:
            if thread.ident is not None:
                thread.join(1)


def test_close_pipe_closes_raw_stream_without_buffered_close() -> None:
    class RawStream:
        closed = False

        def close(self) -> None:
            self.closed = True

    class BufferedStream:
        def __init__(self) -> None:
            self.raw = RawStream()
            self.closed = False

        def fileno(self) -> int:
            raise OSError("no descriptor")

        def close(self) -> None:
            self.closed = True

    stream = BufferedStream()

    runner_module._close_pipe(stream)  # type: ignore[arg-type]

    assert stream.raw.closed is True
    assert stream.closed is False


def test_held_descendant_pipes_close_without_waiting_for_descendant(
    work: Path, logs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen("
        "[sys.executable, '-I', '-c', 'import time; time.sleep(3)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
    )
    pumps: list[runner_module._Pump] = []
    real_start = runner_module._Pump.start

    def start_pump(pump: runner_module._Pump) -> None:
        pumps.append(pump)
        real_start(pump)

    monkeypatch.setattr(runner_module, "_READER_WAIT_S", 0.05)
    monkeypatch.setattr(runner_module, "create_job", lambda: None)
    monkeypatch.setattr(runner_module._Pump, "start", start_pump)

    started = time.monotonic()
    try:
        run(Invocation((sys.executable, "-I", "-c", code)), work, logs)
        assert time.monotonic() - started < 2
        assert len(pumps) == 2
        assert all(not pump.is_alive() for pump in pumps)
    finally:
        if pid_file.exists():
            descendant = int(pid_file.read_text(encoding="utf-8"))
            if pid_alive(descendant):
                os.kill(descendant, 9)
            assert wait_dead(descendant)


def test_failure_after_spawn_kills_process(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_start(_self) -> None:
        raise RuntimeError("reader thread not started")

    monkeypatch.setattr(runner_module._Pump, "start", fail_start)
    silent = Invocation((sys.executable, "-c", "import time; time.sleep(120)"))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="reader thread not started"):
            run(silent, work, logs, on_start=pids.append)
        assert pids and wait_dead(pids[0])
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


def test_second_reader_failure_cleans_first(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_start = runner_module._Pump.start
    readers: list[runner_module._Pump] = []

    def fail_second(self) -> None:
        readers.append(self)
        if len(readers) == 2:
            raise RuntimeError("second reader not started")
        real_start(self)

    monkeypatch.setattr(runner_module._Pump, "start", fail_second)
    silent = Invocation((sys.executable, "-c", "import time; time.sleep(120)"))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="second reader not started"):
            run(silent, work, logs, on_start=pids.append)
        assert pids and wait_dead(pids[0])
        assert readers and readers[0].is_alive() is False
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


def test_on_start_failure_kills_process_and_raises(work: Path, logs: Path) -> None:
    pids: list[int] = []

    def fail_on_start(pid: int) -> None:
        pids.append(pid)
        raise RuntimeError("PID not recorded")

    silent = Invocation((sys.executable, "-c", "import time; time.sleep(120)"))
    try:
        with pytest.raises(RuntimeError, match="PID not recorded"):
            run(silent, work, logs, on_start=fail_on_start)
        assert pids and wait_dead(pids[0])
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


class _BrokenCancel:
    """Cancel flag whose check breaks inside the wait loop (the first check is before launch)."""

    def __init__(self) -> None:
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("failure in the wait loop")
        return False


def test_failure_with_survivor_returns_in_bounded_time(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # process survived the kill and holds the pipe: cleanup does not wait for a busy reader
    silent = Invocation((sys.executable, "-c", "import time; time.sleep(20)"))
    pids: list[int] = []
    started = time.monotonic()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runner_module, "KILL_WAIT_S", 0.2)
            patch.setattr(runner_module, "_READER_WAIT_S", 0.2)
            patch.setattr(runner_module, "create_job", lambda: None)  # the fallback path
            patch.setattr(LocalRunner, "_kill", staticmethod(lambda proc, job=None: None))
            with pytest.raises(RuntimeError, match="failure in the wait loop"):
                run(silent, work, logs, cancel=_BrokenCancel(), on_start=pids.append)
        assert time.monotonic() - started < 5
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


def test_cancel_before_start_does_not_launch(work: Path, logs: Path, tmp_path: Path) -> None:
    marker = tmp_path / "харнес запущен.txt"  # non-ascii on purpose
    cancel = threading.Event()
    cancel.set()
    seen: list[int] = []
    result = run(harness("write", str(marker)), work, logs, cancel=cancel, on_start=seen.append)
    assert result.cancelled is True and result.exit_code is None
    assert seen == []
    assert not marker.exists()
    assert read(result.stdout_path) == "" and read(result.stderr_path) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="process group is checked on POSIX")
def test_dead_parent_live_group_is_killed(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner_module, "KILL_WAIT_S", 0.2)
    monkeypatch.setattr(runner_module, "_READER_WAIT_S", 0.2)
    monkeypatch.setattr(LocalRunner, "_kill", staticmethod(lambda proc, job=None: proc.terminate()))
    pid_file = tmp_path / "child.pid"
    cancel = threading.Event()
    pids: list[int] = []

    def cancel_when_child_started() -> None:
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            if cancel.wait(0.05):
                return
        cancel.set()

    helper = threading.Thread(target=cancel_when_child_started)
    helper.start()
    try:
        result = run(
            harness("child-sleep", "120", str(pid_file)),
            work,
            logs,
            cancel=cancel,
            on_start=pids.append,
        )
        assert result.survived is False
        assert result.cancelled is True
        assert result.exit_code is not None  # parent's exit code is preserved
        assert wait_dead(int(pid_file.read_text(encoding="utf-8")))
    finally:
        cancel.set()
        helper.join(30)
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])
        if pid_file.exists():
            assert wait_dead(int(pid_file.read_text(encoding="utf-8")))


def wait_pid_file(path: Path, cancel: threading.Event | None = None) -> int | None:
    deadline = time.monotonic() + 30
    waiter = threading.Event()
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            pass
        if cancel is None:
            waiter.wait(0.05)
        elif cancel.wait(0.05):
            return None
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_kill_tree_kills_whole_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    inv = harness("child-sleep", "120", str(pid_file))
    proc = subprocess.Popen(
        list(inv.argv),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child_pid: int | None = None
    try:
        child_pid = wait_pid_file(pid_file)
        assert child_pid is not None
        kill_tree(proc.pid)
        assert wait_dead(proc.pid)
        assert wait_dead(child_pid)
    finally:
        kill_tree(proc.pid)
        assert wait_dead(proc.pid)
        if child_pid is not None:
            assert wait_dead(child_pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_timeout_kills_child_processes(work: Path, logs: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    pids: list[int] = []
    child_pid: int | None = None
    try:
        result = run(
            harness("child-sleep", "120", str(pid_file)),
            work,
            logs,
            timeout_s=5,
            on_start=pids.append,
        )
        child_pid = wait_pid_file(pid_file)
        assert child_pid is not None
        assert result.timed_out is True
        assert result.survived is False
        assert wait_dead(child_pid)
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])
        if child_pid is not None:
            assert wait_dead(child_pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_cancel_kills_child_processes(work: Path, logs: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    cancel = threading.Event()
    pids: list[int] = []
    child_pids: list[int] = []

    def cancel_when_child_started() -> None:
        child_pid = wait_pid_file(pid_file, cancel)
        if child_pid is not None:
            child_pids.append(child_pid)
            cancel.set()

    helper = threading.Thread(target=cancel_when_child_started)
    helper.start()
    try:
        result = run(
            harness("child-sleep", "120", str(pid_file)),
            work,
            logs,
            cancel=cancel,
            on_start=pids.append,
        )
        assert result.cancelled is True
        assert result.survived is False
        assert child_pids
        assert wait_dead(child_pids[0])
    finally:
        cancel.set()
        helper.join(30)
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])
        if child_pids:
            assert wait_dead(child_pids[0])
        assert not helper.is_alive()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_harness_runs_in_own_group(work: Path, logs: Path) -> None:
    pids: list[int] = []
    inv = Invocation((sys.executable, "-c", "import os; print(os.getpgid(0))"))
    try:
        result = run(inv, work, logs, on_start=pids.append)
        assert result.exit_code == 0
        assert int(read(result.stdout_path).strip()) != os.getpgid(0)
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_kill_tree_of_dead_pid_is_quiet() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=30)
        kill_tree(proc.pid)
    finally:
        kill_tree(proc.pid)
        assert wait_dead(proc.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_posix_cwd_with_space_and_cyrillic(logs: Path, tmp_path: Path) -> None:
    work = tmp_path / "каталог с пробелом"  # non-ascii on purpose
    work.mkdir()
    marker = work / "метка.txt"  # non-ascii on purpose
    pids: list[int] = []
    try:
        result = run(harness("write", str(marker)), work, logs, on_start=pids.append)
        assert result.exit_code == 0
        assert marker.read_text(encoding="utf-8") == "written by fake harness\n"
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


def test_posix_kill_tree_signals_group_not_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    def fail_kill(_pid: int, _sig: int) -> None:
        raise AssertionError("signal sent to a single PID instead of the group")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(runner_module.os, "kill", fail_kill)
    monkeypatch.setattr(runner_module, "_group_alive", lambda _pid: False)
    kill_tree(4242)
    assert calls == [(4242, runner_module.signal.SIGTERM)]


@pytest.mark.parametrize("error_type", [ProcessLookupError, PermissionError])
def test_posix_kill_tree_suppresses_group_signal_errors(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    def fail_killpg(_pid: int, _sig: int) -> None:
        raise error_type()

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.os, "killpg", fail_killpg, raising=False)
    monkeypatch.setattr(runner_module, "_group_alive", lambda _pid: False)
    kill_tree(4242)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill is used on Windows")
def test_taskkill_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def fake_run(_argv, timeout: float = 30) -> None:
        timeouts.append(timeout)

    monkeypatch.setattr(runner_module, "_run_quiet", fake_run)
    runner_module.kill_tree(12345)
    assert timeouts == [runner_module.KILL_WAIT_S]


# --- windows job object ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-specific")
def test_windows_orphaned_grandchild_is_stopped_on_cancel(
    work: Path, logs: Path, tmp_path: Path
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    cancel = threading.Event()
    grandchild: list[int] = []

    def cancel_when_grandchild_started() -> None:
        pid = wait_pid_file(pid_file, cancel)
        if pid is not None:
            grandchild.append(pid)
            cancel.set()

    helper = threading.Thread(target=cancel_when_grandchild_started)
    helper.start()
    try:
        result = run(
            harness("orphan-sleep", "120", "120", str(pid_file)), work, logs, cancel=cancel
        )
        assert result.cancelled is True
        assert result.survived is False
        assert grandchild and wait_dead(grandchild[0], 10)
    finally:
        cancel.set()
        helper.join(30)
        if not grandchild and pid_file.exists():
            grandchild.append(int(pid_file.read_text(encoding="utf-8")))
        if grandchild:
            if pid_alive(grandchild[0]):
                kill_tree(grandchild[0])
            assert wait_dead(grandchild[0])


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-specific")
def test_windows_grandchild_does_not_outlive_the_harness(
    work: Path, logs: Path, tmp_path: Path
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    grandchild: list[int] = []
    try:
        result = run(harness("orphan-sleep", "0.3", "120", str(pid_file)), work, logs)
        assert result.exit_code == 0 and not result.timed_out and not result.cancelled
        assert result.survived is False
        grandchild.append(int(pid_file.read_text(encoding="utf-8")))
        assert wait_dead(grandchild[0], 10)
    finally:
        if not grandchild and pid_file.exists():
            grandchild.append(int(pid_file.read_text(encoding="utf-8")))
        if grandchild:
            if pid_alive(grandchild[0]):
                kill_tree(grandchild[0])
            assert wait_dead(grandchild[0])


class FakeJob:
    """Stands in for a Windows job object: terminate really stops the assigned process."""

    def __init__(self, *, assign: bool = True, leftovers: Sequence[int] = ()) -> None:
        self.assign_ok = assign
        self.leftovers = list(leftovers)
        self.pid: int | None = None
        self.assigned: list[int] = []
        self.terminated = 0
        self.closed = False
        self.cpu_time_s_value: float | None = None

    def assign(self, pid: int) -> bool:
        self.assigned.append(pid)
        if not self.assign_ok:
            return False
        self.pid = pid
        return True

    def terminate(self) -> None:
        self.terminated += 1
        if self.pid is not None:
            kill_tree(self.pid)

    def cpu_time_s(self) -> float | None:
        return self.cpu_time_s_value

    def pids(self) -> list[int]:
        return list(self.leftovers)

    def close(self) -> None:
        self.closed = True


def with_job(monkeypatch: pytest.MonkeyPatch, **kwargs) -> FakeJob:
    job = FakeJob(**kwargs)
    monkeypatch.setattr(runner_module, "create_job", lambda: job)
    return job


@pytest.mark.skipif(sys.platform != "win32", reason="suspended creation is Windows-specific")
def test_windows_job_is_assigned_before_the_process_runs(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch)
    real_popen = runner_module.subprocess.Popen
    real_assign = job.assign
    real_resume = runner_module.resume_process
    creation_flags: list[int] = []
    events: list[str] = []

    def assign_spy(pid: int) -> bool:
        events.append("assign")
        return real_assign(pid)

    def resume_spy(pid: int) -> None:
        events.append("resume")
        real_resume(pid)

    def popen_spy(*args, **kwargs):
        creation_flags.append(kwargs.get("creationflags", 0))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(job, "assign", assign_spy)
    monkeypatch.setattr(runner_module, "resume_process", resume_spy)
    monkeypatch.setattr(runner_module.subprocess, "Popen", popen_spy)
    result = run(harness("sleep", "0.1"), work, logs, on_start=lambda _pid: events.append("start"))
    assert result.exit_code == 0
    assert creation_flags[0] & 0x00000004  # CREATE_SUSPENDED closes the assignment race
    assert job.assigned
    assert events == ["assign", "resume", "start"]


def test_job_stops_the_tree_on_timeout(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch)
    pids: list[int] = []
    result = run(harness("sleep", "120"), work, logs, timeout_s=1, on_start=pids.append)
    assert result.timed_out and not result.cancelled
    assert result.survived is False
    assert job.assigned == pids
    assert job.terminated >= 1
    assert job.closed is True
    assert wait_dead(pids[0])


def test_job_stops_the_tree_on_cancel(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch)
    cancel = threading.Event()
    pids: list[int] = []

    def on_start(pid: int) -> None:
        pids.append(pid)
        cancel.set()

    result = run(harness("sleep", "120"), work, logs, cancel=cancel, on_start=on_start)
    assert result.cancelled and not result.timed_out
    assert result.survived is False
    assert job.terminated >= 1 and job.closed is True
    assert wait_dead(pids[0])


def test_job_is_terminated_after_the_harness_exits(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch)
    result = run(harness("sleep", "0.1"), work, logs)
    assert result.exit_code == 0 and not result.timed_out
    assert job.terminated >= 1 and job.closed is True


def test_leftover_process_in_the_job_is_a_survivor(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch, leftovers=[999_999])
    with monkeypatch.context() as patch:
        patch.setattr(runner_module, "KILL_WAIT_S", 0.2)
        result = run(harness("sleep", "0.1"), work, logs)
    assert result.exit_code == 0 and result.survived is True
    assert job.closed is True


def test_job_assign_failure_falls_back_to_the_tree_kill(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch, assign=False)
    pids: list[int] = []
    result = run(harness("sleep", "120"), work, logs, timeout_s=1, on_start=pids.append)
    assert result.timed_out and result.survived is False
    assert job.terminated == 0 and job.closed is True
    assert wait_dead(pids[0])  # the taskkill fallback still stopped the process


def test_job_is_closed_when_the_launch_fails(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = with_job(monkeypatch)
    missing = Invocation((str(tmp_path / "no-such-harness"),))
    with pytest.raises(OSError):
        run(missing, work, logs)
    assert job.closed is True


def test_job_is_closed_after_a_runner_failure(
    work: Path, logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = with_job(monkeypatch)

    def fail_start(_self) -> None:
        raise RuntimeError("reader thread not started")

    monkeypatch.setattr(runner_module._Pump, "start", fail_start)
    silent = Invocation((sys.executable, "-c", "import time; time.sleep(120)"))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="reader thread not started"):
            run(silent, work, logs, on_start=pids.append)
        assert job.closed is True
        assert pids and wait_dead(pids[0])
    finally:
        if pids:
            kill_tree(pids[0])
            assert wait_dead(pids[0])


def test_job_survivor_check_reads_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(runner_module, "KILL_WAIT_S", 0.2)
        assert runner_module._survived(4242, parent_alive=False, job=FakeJob()) is False
        assert runner_module._survived(4242, parent_alive=False, job=FakeJob(leftovers=[7])) is True
        assert runner_module._survived(4242, parent_alive=True, job=FakeJob()) is True


def test_job_with_unreadable_list_counts_as_a_survivor(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unreadable(FakeJob):
        def pids(self) -> list[int]:
            raise JobQueryError("job process list not read")

    with monkeypatch.context() as patch:
        patch.setattr(runner_module, "KILL_WAIT_S", 0.2)
        assert runner_module._survived(4242, parent_alive=False, job=Unreadable()) is True


# --- process creation time ----------------------------------------------------------------


def test_filetime_to_epoch() -> None:
    base = 116444736000000000  # 1970-01-01 in 100-nanosecond intervals since 1601
    assert runner_module._filetime_to_epoch(base & 0xFFFFFFFF, base >> 32) == 0.0
    later = base + 10_000_000
    assert runner_module._filetime_to_epoch(later & 0xFFFFFFFF, later >> 32) == 1.0


def test_process_started_of_own_process() -> None:
    first = runner_module.process_started(os.getpid())
    second = runner_module.process_started(os.getpid())
    now = time.time()
    assert first is not None and second is not None
    assert now - 30 * 86400 <= first <= now + 1
    assert abs(first - second) <= runner_module.PROC_START_TOLERANCE_S


def test_process_started_of_missing_pid_is_none() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(30)
    assert wait_dead(child.pid)
    # process has exited, but the Popen handle is still open (on Windows the PID then looks "open")
    assert runner_module.process_started(child.pid) is None
    assert runner_module.process_started(2**22 + 12345) is None


@pytest.mark.parametrize(
    ("line", "moment"),
    [
        ("Sat Nov 14 12:00:00 2026", (2026, 11, 14, 12, 0, 0)),
        ("Sat Nov  7 09:05:03 2026", (2026, 11, 7, 9, 5, 3)),
    ],
)
def test_posix_process_started_parses_lstart(
    monkeypatch: pytest.MonkeyPatch, line: str, moment: tuple[int, ...]
) -> None:
    seen: list[tuple[list[str], float, dict[str, str]]] = []

    def fake_run(argv, timeout: float = 30, env=None):
        seen.append((list(argv), timeout, dict(env or {})))
        return f"{line}\n"

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module, "_run_quiet", fake_run)
    assert runner_module.process_started(4242) == calendar.timegm(moment)
    ((argv, timeout, env),) = seen
    assert argv == ["ps", "-p", "4242", "-o", "lstart="]
    assert timeout <= runner_module.QUERY_WAIT_S
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC0"


def test_posix_started_is_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def fake_run(argv, timeout: float = 30, env=None):
        seen.append(dict(env or {}))
        return "Sun Nov 15 10:00:00 2026\n"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("creation time depends on the local time zone")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module, "_run_quiet", fake_run)
    monkeypatch.setattr(runner_module.time, "mktime", forbidden)

    assert runner_module.process_started(4242) == calendar.timegm((2026, 11, 15, 10, 0, 0))
    (env,) = seen
    assert env["TZ"] == "UTC0"
    assert env["LC_ALL"] == "C"


@pytest.mark.parametrize(
    "error", [ctypes.ArgumentError("bad argument"), OverflowError("too big")], ids=type
)
def test_process_started_ctypes_errors_are_none(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def open_process(*_args):
        raise error

    def answer(*_args):
        return 0

    kernel32 = types.SimpleNamespace(
        OpenProcess=open_process,
        GetProcessTimes=answer,
        GetExitCodeProcess=answer,
        CloseHandle=answer,
    )
    monkeypatch.setattr(runner_module.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)

    assert runner_module.process_started(4242) is None


GARBAGE = "мусор"  # non-ascii on purpose


@pytest.mark.parametrize(
    "line", ["", GARBAGE, "Sat Foo 14 12:00:00 2026", "Sat Nov 14 12:00 2026", "Sat Nov 14"]
)
def test_posix_process_started_of_unparsed_line_is_none(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module, "_run_quiet", lambda argv, timeout=30, env=None: line)
    assert runner_module.process_started(4242) is None


def test_posix_process_started_without_ps_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module, "_run_quiet", lambda argv, timeout=30, env=None: None)
    assert runner_module.process_started(4242) is None


def test_posix_lstart_parse_ignores_python_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("parsing depends on the Python process locale")

    monkeypatch.setattr(runner_module.sys, "platform", "linux")
    monkeypatch.setattr(runner_module.time, "strptime", forbidden)
    monkeypatch.setattr(
        runner_module,
        "_run_quiet",
        lambda argv, timeout=30, env=None: "Sat Nov 14 12:00:00 2026\n",
    )
    assert runner_module.process_started(4242) == calendar.timegm((2026, 11, 14, 12, 0, 0))
