"""Dispatcher helpers that need no dispatcher state: lock keys, output tails, route snapshots."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from agent_router import worktree
from agent_router.jobs import Job, directory_key
from agent_router.policy import Choice, Decision, RouteMetrics

LOG_TAIL_CHARS = 4000
ERROR_TEXT_MAX = 500
_CWD_CHANGED = "job directory changed after validation"
_READ_MAX_BYTES = 20 * 2**20 + 4096


def _repo_key(root: Path | None) -> str | None:
    return None if root is None else os.path.normcase(str(root))


def _writer_key(root: Path | None) -> str | None:
    """Writer lock key: one for every spelling of a repository, whatever the volume's case rules."""
    return None if root is None else directory_key(root)


def _cwd_matches(job: Job, checked: Path) -> bool:
    try:
        current = Path(job.request.cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return _repo_key(current) == _repo_key(checked)


def _read(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(_READ_MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _tail(stdout: str, stderr: str) -> str:
    joined = stdout if not stderr.strip() else f"{stdout}\n[stderr]\n{stderr}"
    return joined[-LOG_TAIL_CHARS:]


def _safe_error(text: str, cwd: Path) -> str:
    safe = worktree.without_paths(text, cwd)
    return safe if len(safe) <= ERROR_TEXT_MAX else safe[:ERROR_TEXT_MAX] + "…"


def _measurement_tail(tail: str | None, exc: Exception) -> str:
    detail = str(exc) if isinstance(exc, worktree.WorktreeError) else type(exc).__name__
    line = f"[agent-router: post-run measurement failed: {detail}]"
    return f"{tail}\n{line}"[-LOG_TAIL_CHARS:] if tail else line[-LOG_TAIL_CHARS:]


_METRIC_FIELDS = (
    "pool",
    "priority",
    "weight",
    "lag",
    "urgency",
    "score",
    "quality",
    "has_data",
    "prefer_rank",
)


def reason_with_note(choice: Choice, decision: Decision) -> str:
    """The choice reason with the decision's note appended, e.g. a review fallback."""
    if decision.note is None:
        return choice.reason
    return f"{choice.reason}; {decision.note}"


def decision_snapshot(job: Job, decision: Decision, at: datetime) -> dict:
    """Route choice snapshot for an attempt: candidates with metrics and the excluded ones."""

    def metrics(metrics: RouteMetrics | None) -> dict:
        if metrics is None:
            return {}
        return {name: getattr(metrics, name) for name in _METRIC_FIELDS}

    def entry(choice: Choice) -> dict:
        return {
            "route": choice.route.id,
            "model": choice.route.model,
            "note": choice.note,
            **metrics(decision.metrics.get(choice.route.id)),
        }

    chosen = decision.chosen[0] if decision.chosen else None
    return {
        "at": at.isoformat(),
        "tier": job.tier,
        "tier_source": job.tier_source,
        "mode": job.request.mode,
        "caller": job.request.caller,
        "chosen": None if chosen is None else entry(chosen),
        "candidates": [entry(choice) for choice in decision.chosen],
        "excluded": [{"route": item.route.id, "reason": item.reason} for item in decision.excluded],
        "note": decision.note,
    }
