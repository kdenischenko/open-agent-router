"""Windows job objects: everything an attempt starts stops with the attempt."""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_EXTENDED_LIMIT_INFORMATION = 9
_BASIC_PROCESS_ID_LIST = 3
_PID_LIST_SIZE = 1024
_TERMINATE_EXIT_CODE = 1
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_RESUME_FAILED = 0xFFFFFFFF


class JobUnavailable(Exception):
    """A job object could not be created or configured."""


class JobQueryError(Exception):
    """The list of processes in a job could not be read."""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessIdList(ctypes.Structure):
    """JOBOBJECT_BASIC_PROCESS_ID_LIST: the entries are ULONG_PTR, so pointer-sized."""

    _fields_ = [
        ("NumberOfAssignedProcesses", ctypes.c_ulong),
        ("NumberOfProcessIdsInList", ctypes.c_ulong),
        ("ProcessIdList", ctypes.c_size_t * _PID_LIST_SIZE),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ThreadID", ctypes.c_ulong),
        ("th32OwnerProcessID", ctypes.c_ulong),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
    ]


def _declare(kernel32) -> None:
    """Argument types of the calls this module makes; a plain object can stand in for the DLL."""
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Thread32First.restype = ctypes.c_int
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Thread32Next.restype = ctypes.c_int
    kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel32.ResumeThread.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int


def _main_thread_id(kernel32, pid: int) -> int:
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE:
        raise JobUnavailable(f"thread snapshot not created: error {ctypes.get_last_error()}")
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if int(entry.th32OwnerProcessID) == int(pid):
                return int(entry.th32ThreadID)
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        raise JobUnavailable(f"main thread not found for process {pid}")
    finally:
        kernel32.CloseHandle(snapshot)


def resume_process(pid: int) -> None:
    """Resume the initial thread of a suspended process; do nothing away from Windows."""
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _declare(kernel32)
    thread_id = _main_thread_id(kernel32, pid)
    thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        raise JobUnavailable(f"thread not opened: error {ctypes.get_last_error()}")
    try:
        while True:
            previous = kernel32.ResumeThread(thread)
            if previous == _RESUME_FAILED:
                code = ctypes.get_last_error()
                raise JobUnavailable(f"thread not resumed: error {code}")
            if previous <= 1:
                break
    finally:
        kernel32.CloseHandle(thread)


class WindowsJob:
    """One attempt's process tree inside a Windows job object.

    Every process the harness starts joins the job, so a single terminate stops the whole
    tree, and kill on job close stops what is left when the last handle to the job closes.
    """

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare(kernel32)
        self._kernel32 = kernel32
        self._handle: int | None = None
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise JobUnavailable(f"job object not created: error {ctypes.get_last_error()}")
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _EXTENDED_LIMIT_INFORMATION,
            ctypes.pointer(limits),
            ctypes.sizeof(limits),
        ):
            code = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise JobUnavailable(f"job limits not set: error {code}")
        self._handle = handle

    def assign(self, pid: int) -> bool:
        """Put a started process into the job; False when that is not possible."""
        if self._handle is None:
            return False
        process = self._kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid)
        )
        if not process:
            return False
        try:
            assigned = bool(self._kernel32.AssignProcessToJobObject(self._handle, process))
        finally:
            self._kernel32.CloseHandle(process)
        if not assigned:
            log.debug("process %d not assigned to the job", pid)
        return assigned

    def terminate(self) -> None:
        """Stop every process in the job; what survives dies when the handle closes."""
        if self._handle is None:
            return
        self._kernel32.TerminateJobObject(self._handle, _TERMINATE_EXIT_CODE)

    def pids(self) -> tuple[int, ...]:
        """The processes currently in the job; JobQueryError when the list cannot be read."""
        if self._handle is None:
            return ()
        listing = _ProcessIdList()
        if not self._query(listing):
            raise JobQueryError(f"job process list not read: error {ctypes.get_last_error()}")
        # A process that already exited keeps a zero entry until the job forgets it: only
        # real PIDs count, so the list empties when the tree is gone
        count = min(int(listing.NumberOfProcessIdsInList), _PID_LIST_SIZE)
        listed = (listing.ProcessIdList[index] for index in range(count))
        return tuple(pid for pid in listed if pid)

    def close(self) -> None:
        """Release the handle: kill on job close then stops whatever is left in the job."""
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)

    def _query(self, listing: _ProcessIdList) -> bool:
        """Fill the listing with the job's current process ids."""
        returned = ctypes.c_ulong(0)
        return bool(
            self._kernel32.QueryInformationJobObject(
                self._handle,
                _BASIC_PROCESS_ID_LIST,
                ctypes.byref(listing),
                ctypes.sizeof(listing),
                ctypes.byref(returned),
            )
        )


def create_job() -> WindowsJob | None:
    """A job object for one attempt; None when there is none to use."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsJob()
    except (JobUnavailable, OSError) as exc:
        log.debug("job object unavailable: %s", exc)
        return None
