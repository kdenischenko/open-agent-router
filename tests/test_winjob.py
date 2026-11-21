"""Windows job objects with the Win32 boundary stubbed."""

from __future__ import annotations

import ctypes

import pytest

from agent_router import winjob


class _Function:
    def __init__(self, result=1) -> None:
        self.result = result
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if callable(self.result):
            return self.result(*args)
        return self.result


class _Kernel32:
    def __init__(self) -> None:
        self.CreateJobObjectW = _Function(100)
        self.SetInformationJobObject = _Function()
        self.AssignProcessToJobObject = _Function()
        self.TerminateJobObject = _Function()
        self.QueryInformationJobObject = _Function()
        self.OpenProcess = _Function(200)
        self.CreateToolhelp32Snapshot = _Function(300)
        self.Thread32First = _Function(0)
        self.Thread32Next = _Function(0)
        self.OpenThread = _Function(400)
        self.ResumeThread = _Function(1)
        self.CloseHandle = _Function()

    @property
    def closed(self) -> list[int]:
        return [call[0] for call in self.CloseHandle.calls]


def make_job(
    monkeypatch: pytest.MonkeyPatch, dll: _Kernel32 | None = None
) -> tuple[winjob.WindowsJob, _Kernel32]:
    dll = dll or _Kernel32()
    monkeypatch.setattr(winjob.ctypes, "WinDLL", lambda *_args, **_kwargs: dll, raising=False)
    monkeypatch.setattr(winjob.ctypes, "get_last_error", lambda: 5, raising=False)
    return winjob.WindowsJob(), dll


def use_dll(monkeypatch: pytest.MonkeyPatch, dll: _Kernel32) -> None:
    monkeypatch.setattr(winjob.sys, "platform", "win32")
    monkeypatch.setattr(winjob.ctypes, "WinDLL", lambda *_args, **_kwargs: dll, raising=False)
    monkeypatch.setattr(winjob.ctypes, "get_last_error", lambda: 5, raising=False)


def thread(owner: int, thread_id: int):
    def fill(_snapshot, output) -> int:
        entry = output._obj
        assert entry.dwSize == ctypes.sizeof(winjob._ThreadEntry)
        entry.th32OwnerProcessID = owner
        entry.th32ThreadID = thread_id
        return 1

    return fill


def test_job_is_configured_to_kill_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    job, dll = make_job(monkeypatch)
    args = dll.SetInformationJobObject.calls[0]
    assert args[0] == 100
    assert args[2].contents.BasicLimitInformation.LimitFlags == (
        winjob.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    job.close()


def test_configuration_failure_closes_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    dll = _Kernel32()
    dll.SetInformationJobObject = _Function(0)
    with pytest.raises(winjob.JobUnavailable, match="job limits not set"):
        make_job(monkeypatch, dll)
    assert dll.closed == [100]


@pytest.mark.parametrize("assigned", [True, False])
def test_assignment_closes_the_process_handle(
    monkeypatch: pytest.MonkeyPatch, assigned: bool
) -> None:
    job, dll = make_job(monkeypatch)
    dll.AssignProcessToJobObject = _Function(assigned)
    assert job.assign(42) is assigned
    assert dll.AssignProcessToJobObject.calls == [(100, 200)]
    assert dll.closed == [200]


def test_open_process_failure_means_no_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    job, dll = make_job(monkeypatch)
    dll.OpenProcess = _Function(0)
    assert job.assign(42) is False
    assert not dll.AssignProcessToJobObject.calls
    assert not dll.closed


def test_terminate_and_idempotent_close(monkeypatch: pytest.MonkeyPatch) -> None:
    job, dll = make_job(monkeypatch)
    job.terminate()
    job.close()
    job.terminate()
    job.close()
    assert dll.TerminateJobObject.calls == [(100, 1)]
    assert dll.closed == [100]
    assert job.pids() == ()


def test_pids_returns_only_live_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    job, dll = make_job(monkeypatch)

    def list_pids(_handle, _kind, output, _size, _returned) -> int:
        listing = output._obj
        listing.NumberOfProcessIdsInList = 3
        listing.ProcessIdList[:3] = [11, 0, 22]
        return 1

    dll.QueryInformationJobObject = _Function(list_pids)
    assert job.pids() == (11, 22)


def test_pid_query_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    job, dll = make_job(monkeypatch)
    dll.QueryInformationJobObject = _Function(0)
    with pytest.raises(winjob.JobQueryError, match="job process list not read"):
        job.pids()


def test_create_job_is_a_noop_away_from_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winjob.sys, "platform", "linux")

    def unexpected():
        raise AssertionError("WindowsJob must not be constructed")

    monkeypatch.setattr(winjob, "WindowsJob", unexpected)
    assert winjob.create_job() is None


def test_create_job_unavailable_uses_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winjob.sys, "platform", "win32")

    def unavailable():
        raise winjob.JobUnavailable("nested job rejected")

    monkeypatch.setattr(winjob, "WindowsJob", unavailable)
    assert winjob.create_job() is None


def test_resume_skips_other_processes_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dll = _Kernel32()
    dll.Thread32First = _Function(thread(7, 500))
    dll.Thread32Next = _Function(thread(42, 501))
    use_dll(monkeypatch, dll)
    winjob.resume_process(42)
    assert dll.OpenThread.calls == [(winjob._THREAD_SUSPEND_RESUME, False, 501)]
    assert dll.ResumeThread.calls == [(400,)]
    assert dll.closed == [300, 400]


def test_resume_failure_still_closes_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    dll = _Kernel32()
    dll.Thread32First = _Function(thread(42, 501))
    dll.ResumeThread = _Function(winjob._RESUME_FAILED)
    use_dll(monkeypatch, dll)
    with pytest.raises(winjob.JobUnavailable, match="thread not resumed"):
        winjob.resume_process(42)
    assert dll.closed == [300, 400]


def test_snapshot_failure_does_not_close_an_invalid_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dll = _Kernel32()
    dll.CreateToolhelp32Snapshot = _Function(winjob._INVALID_HANDLE)
    use_dll(monkeypatch, dll)
    with pytest.raises(winjob.JobUnavailable, match="thread snapshot not created"):
        winjob.resume_process(42)
    assert not dll.closed


def test_resume_is_a_noop_away_from_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winjob.sys, "platform", "linux")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("WinDLL must not be loaded")

    monkeypatch.setattr(winjob.ctypes, "WinDLL", unexpected, raising=False)
    winjob.resume_process(42)
