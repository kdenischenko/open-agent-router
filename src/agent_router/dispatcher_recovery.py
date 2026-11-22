"""Startup recovery: recorded outcomes, interrupted attempts, and orphan process reaping."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable

from agent_router import worktree
from agent_router.dispatcher_support import _CWD_CHANGED, _cwd_matches, _measurement_tail
from agent_router.jobs import Attempt, Job
from agent_router.runner import PROC_START_TOLERANCE_S, ProcessQueryError

REAP_RETRY_PAUSE_S = 0.5  # pause before retrying the failed first process query in _reap
_RECOVER_LIST = 1000

log = logging.getLogger(__name__)


class RecoverFailed(Exception):
    def __init__(self, job_ids: tuple[str, ...]) -> None:
        super().__init__(f"recovery failed for jobs: {', '.join(job_ids)}")
        self.job_ids = job_ids


def reap_process(
    attempt: Attempt,
    *,
    image: Callable[[int], str | None],
    started: Callable[[int], float | None],
    kill: Callable[[int], None],
    wait_s: float,
) -> bool:
    """Kills only a process with a confirmed image and creation time."""
    pid = attempt.pid
    if pid is None:
        return True
    deadline = time.monotonic() + wait_s
    try:
        name = image(pid)
    except ProcessQueryError:
        time.sleep(REAP_RETRY_PAUSE_S)  # one-off query failure under load: a single retry
        try:
            name = image(pid)
        except ProcessQueryError:
            return False
    if name is None:
        return True
    if attempt.proc_started is None:
        return False
    if attempt.exe_name and name.lower() != attempt.exe_name.lower():
        return True
    current_start = started(pid)
    if current_start is None:
        return False
    if abs(current_start - attempt.proc_started) > PROC_START_TOLERANCE_S:
        return True  # the PID belongs to a foreign process: leave it alone
    if not attempt.exe_name:
        return False  # our process, but the image is unconfirmed: do not kill
    expected = attempt.exe_name.lower()

    def alive() -> bool:
        try:
            current_name = image(pid)
        except ProcessQueryError:
            return True
        return current_name is not None and current_name.lower() == expected

    with contextlib.suppress(Exception):
        kill(pid)
    while True:  # one check after the kill is mandatory even if queries ate the deadline
        if not alive():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class RecoveryMixin:
    """Startup recovery for a host providing job state, process probes, and outcome helpers.

    The host supplies `jobs`, `clock`, `_lock`, `_checked_cwds`, `orphan_wait_s`, `_image`,
    `_started`, `_kill`, `_active_snapshot`, `_apply`, `_changes`, `_finalize_attempt`, and
    `_emergency`.
    """

    def recover(self) -> None:
        """On startup, apply the recorded outcome or finish a running job as an emergency."""
        failed: list[str] = []
        with self._lock:
            active_ids = {active.job_id for active in self._active_snapshot()}
            for job in self.jobs.list("running", limit=_RECOVER_LIST):
                if job.id in active_ids:
                    continue
                try:
                    attempts = self.jobs.attempts(job.id)
                    unfinished = [a for a in attempts if a.finished_at is None]
                    if not unfinished and attempts and attempts[-1].result is not None:
                        last = attempts[-1]
                        assert last.result is not None
                        self._apply(
                            job,
                            last.pool,
                            last.result,
                            shutdown=last.result.outcome == "cancelled",
                        )
                        continue
                    self._recover_emergency(job, attempts)
                except Exception:
                    failed.append(job.id)
                    log.exception("job recovery failed: %s", job.id)
        if failed:
            raise RecoverFailed(tuple(failed))

    def _recover_emergency(self, job: Job, attempts: list[Attempt]) -> None:
        """Shared handling of unfinished attempts and of a finished record without an outcome."""
        changed: set[str] = set()
        moved = False
        unknown = False
        in_git = job.repo_root is not None
        checked = self._checked_cwds.get(job.id)
        violations = list(job.violations)
        unfinished = [a for a in attempts if a.finished_at is None]
        for attempt in unfinished or attempts[-1:]:
            if (
                attempt.finished_at is None
                and not self._reap(attempt)
                and "orphan_process" not in violations
            ):
                violations.append("orphan_process")
            if in_git and attempt.tree_before is not None:
                try:
                    if checked is not None and not _cwd_matches(job, checked):
                        raise worktree.WorktreeError(_CWD_CHANGED)
                    # Without the in-memory intake path, stay inside the stored repository
                    cwd = checked or job.repo_root
                    assert cwd is not None
                    files, head_moved = self._changes(cwd, attempt)
                    changed.update(files)
                    moved = moved or head_moved
                except Exception as exc:  # tree not measured: changes unknown
                    unknown = True
                    attempt.log_tail = _measurement_tail(attempt.log_tail, exc)
            if attempt.finished_at is None:
                attempt.finished_at = self.clock.now()
                attempt.outcome = "gateway_restarted"
                self._finalize_attempt(attempt)
        if in_git:
            job.changed_files = tuple(sorted(changed | set(job.changed_files or ())))
        if moved and "head_moved" not in violations:
            violations.append("head_moved")
        read_only = job.request.mode != "write"
        if read_only and changed and "read_mode_files_changed" not in violations:
            violations.append("read_mode_files_changed")
        if unknown and "changes_unknown" not in violations:
            violations.append("changes_unknown")
        job.violations = tuple(violations)
        self._emergency(
            job,
            "gateway_restarted",
            "gateway was restarted during an attempt",
            # a surviving process may have kept writing: for a writer that counts as a change
            bool(changed) or moved or unknown or "orphan_process" in violations,
        )

    def _reap(self, attempt: Attempt) -> bool:
        return reap_process(
            attempt,
            image=self._image,
            started=self._started,
            kill=self._kill,
            wait_s=self.orphan_wait_s,
        )
