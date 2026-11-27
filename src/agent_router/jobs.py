"""Gateway job: request, statuses and their transitions, intake checks."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agent_router.config import TIERS, Config, Mode, SpendSource, TaskSettings, Tier
from agent_router.worktree import TreeSnapshot, WorktreeError, repo_root

Status = Literal[
    "queued",
    "awaiting_approval",
    "running",
    "completed",
    "partial",
    "failed",
    "declined",
    "cancelled",
]
STATUSES: tuple[Status, ...] = (
    "queued",
    "awaiting_approval",
    "running",
    "completed",
    "partial",
    "failed",
    "declined",
    "cancelled",
)
TERMINAL: frozenset[Status] = frozenset({"completed", "partial", "failed", "declined", "cancelled"})
MODES: tuple[Mode, ...] = ("read", "write")
KINDS: tuple[str, ...] = ("code", "review", "research", "other")
TierSource = Literal["caller", "default", "classifier"]
TIMEOUT_MIN_S = 30
TIMEOUT_MAX_S = 7200
# an idempotent submit key: safe in URLs and file names, never ambiguous
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# Allowed status transitions; no others exist.
_TRANSITIONS: dict[Status, frozenset[Status]] = {
    "queued": frozenset({"running", "awaiting_approval", "failed", "declined", "cancelled"}),
    "awaiting_approval": frozenset({"queued", "declined", "cancelled"}),
    "running": frozenset({"completed", "partial", "failed", "queued", "cancelled"}),
}


@dataclass(frozen=True)
class JobRequest:
    prompt: str
    cwd: Path
    mode: Mode = "read"
    tier: Tier | None = None
    kind: str | None = None
    caller: str | None = None
    route: str | None = None
    timeout_s: int | None = None
    excluded_pools: frozenset[str] = frozenset()
    preferred_routes: tuple[str, ...] = ()  # routes tried first, in order; others may still run
    review_of: str | None = None  # id of the job this review job looks at; requires kind=review
    # idempotency key of the caller: a repeated submit with the same key gets the existing job
    request_id: str | None = None


@dataclass
class Job:
    id: str
    request: JobRequest
    status: Status
    tier: Tier
    tier_source: TierSource
    repo_root: Path | None
    route: str | None = None
    reason: str | None = None
    approval_route: str | None = None  # route awaiting a human decision
    approval_deadline: datetime | None = None
    approved_route: str | None = None  # locked in after approval
    had_refusal: bool = False  # a refusal means no routes left -> declined
    excluded_routes: frozenset[str] = frozenset()
    excluded_pools: frozenset[str] = frozenset()
    cancel_requested: bool = False
    text: str | None = None
    changed_files: tuple[str, ...] | None = None
    violations: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    classifier_tier: Tier | None = None
    classifier_confidence: float | None = None
    runner: str = "local"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    prompt_sha256: str | None = None  # hex sha256 of the UTF-8 prompt; same prompt, same hash
    prompt_chars: int | None = None  # prompt length in characters
    tier_pending: bool = False  # tier undecided: awaiting the primary vote or the wait_s deadline


@dataclass(frozen=True)
class Rating:
    """Job quality rating; the note text is not stored, only its hash and length."""

    job_id: str
    value: Literal["good", "bad", "partial"]
    should_tier: Tier | None
    by: str | None
    note_sha256: str | None
    note_chars: int | None
    at: datetime


RATING_VALUES: tuple[str, ...] = ("good", "bad", "partial")


@dataclass(frozen=True)
class Vote:
    """A single classifier's vote on a job; the prompt text is not part of it."""

    classifier_id: str
    model: str
    tier: Tier | None
    confidence: float | None
    latency_ms: int
    error: str | None
    offline: bool = False


@dataclass(frozen=True)
class VoteRow:
    """A vote joined to its job and the job's latest rating (for reports and export)."""

    job_id: str
    classifier_id: str
    model: str
    tier: Tier | None
    confidence: float | None
    latency_ms: int
    error: str | None
    offline: bool
    at: datetime
    job_tier: Tier | None
    tier_source: str | None
    mode: str | None
    created_at: datetime
    rating_value: str | None
    rating_should_tier: Tier | None


@dataclass
class Attempt:
    job_id: str
    n: int
    route: str
    pool: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str | None = None
    duration_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    head_before: str | None = None
    tree_before: TreeSnapshot | None = None
    log_tail: str | None = None
    pid: int | None = None
    exe_name: str | None = None
    proc_started: float | None = None
    result: AttemptResult | None = None  # full outcome; None — unfinished or an old-format row
    spend_source: SpendSource | None = None
    decision: dict | None = None  # snapshot of the route decision; None — an old row
    cost_estimated: bool = False  # cost_usd estimated from price_file, not reported by the harness


Outcome = Literal["ok", "limit", "error", "timeout", "cancelled"]
OUTCOMES: tuple[Outcome, ...] = ("ok", "limit", "error", "timeout", "cancelled")


@dataclass(frozen=True)
class AttemptResult:
    """Attempt outcome: the worker thread hands it to the dispatcher, which sets the job status."""

    outcome: Outcome
    text: str = ""
    error: str | None = None
    limit_reset_at: datetime | None = None
    changed: tuple[str, ...] | None = None  # None: directory outside git, changes not counted
    head_moved: bool = False
    changes_unknown: bool = False  # measuring the tree after the attempt failed
    survived: bool = False  # the harness process survived the stop
    model_unavailable: bool = False  # the harness reported the route's model as unavailable
    content_rejected: bool = False  # the provider refused the request on content grounds


class RequestRejected(Exception):
    """Request failed intake validation; `code` is the error code for the 422 response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WrongState(Exception):
    """Action does not fit the job's current status (409 response)."""


def can_transition(a: Status, b: Status) -> bool:
    return b in _TRANSITIONS.get(a, frozenset())


def new_job_id() -> str:
    return "j_" + secrets.token_hex(6)


def _inside(path: str, root: str) -> bool:
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)


def _identity(path: Path) -> tuple[int, int] | None:
    """Volume and file number of an existing directory, as the filesystem itself sees it.

    A name comparison cannot tell whether two spellings are one directory: macOS volumes
    ignore case and Unicode normalization, while `os.path.normcase` there changes nothing.
    None when the path is missing or the filesystem reports no file number.
    """
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    if info.st_ino == 0:
        return None
    return (info.st_dev, info.st_ino)


def directory_key(path: Path) -> str:
    """Key that is equal for every spelling of one existing directory."""
    identity = _identity(path)
    if identity is None:
        return os.path.normcase(str(path))
    return f"{identity[0]}:{identity[1]}"


class _Lineage:
    """The resolved job directory and its parents, compared with roots by name and by identity."""

    def __init__(self, resolved: Path) -> None:
        self.name = os.path.normcase(str(resolved))
        self._paths = (resolved, *resolved.parents)
        self._identities: set[object] | None = None

    def under(self, root: Path) -> bool:
        resolved = root.resolve()
        if _inside(self.name, os.path.normcase(str(resolved))):
            return True
        identity = _identity(resolved)
        if identity is None:
            return False
        if self._identities is None:
            found = (_identity(path) for path in self._paths)
            self._identities = {item for item in found if item is not None}
        return identity in self._identities


def is_network_path(text: str) -> bool:
    """Path starting with two separators: a UNC or device path."""
    return len(text) >= 2 and text[0] in "\\/" and text[1] in "\\/"


def check_cwd(cwd: Path, tasks: TaskSettings) -> Path:
    """Job directory exists, is inside allowed roots and outside denied ones.

    Returns the directory with symlinks and `..` resolved; otherwise raises
    RequestRejected("cwd_not_allowed"). The dispatcher repeats the check before each attempt.
    """
    raw = str(cwd)
    if is_network_path(raw):
        raise RequestRejected("cwd_not_allowed", "network and device paths are not accepted")
    if not Path(raw).is_absolute():
        raise RequestRejected("cwd_not_allowed", "an absolute directory path is required")
    try:
        resolved = Path(raw).resolve()
        is_dir = resolved.is_dir()
    except (OSError, RuntimeError, ValueError) as exc:
        message = f"job directory is not accessible: {type(exc).__name__}"
        raise RequestRejected("cwd_not_allowed", message) from exc
    if not is_dir:
        raise RequestRejected("cwd_not_allowed", "no such directory")
    lineage = _Lineage(resolved)
    if is_network_path(lineage.name):
        raise RequestRejected("cwd_not_allowed", "network and device paths are not accepted")
    if not tasks.allowed_roots:
        raise RequestRejected("cwd_not_allowed", "allowed_roots is empty: jobs are not accepted")
    if not any(lineage.under(root) for root in tasks.allowed_roots):
        raise RequestRejected("cwd_not_allowed", "directory outside allowed roots")
    if any(lineage.under(root) for root in tasks.denied_roots):
        raise RequestRejected("cwd_not_allowed", "directory inside a denied root")
    return resolved


@dataclass(frozen=True)
class CheckedDirectory:
    """The directory and repository root accepted by request validation."""

    cwd: Path
    repo_root: Path | None


def validate_request_directory(req: JobRequest, config: Config) -> CheckedDirectory:
    """Intake checks. Returns the resolved directory and its git working tree root."""
    tasks = config.tasks
    if not isinstance(req.prompt, str) or not req.prompt.strip():
        raise RequestRejected("bad_prompt", "empty prompt")
    if len(req.prompt) > tasks.max_prompt_chars:
        raise RequestRejected(
            "bad_prompt", f"prompt longer than {tasks.max_prompt_chars} characters"
        )
    if req.mode not in MODES:
        raise RequestRejected("bad_mode", f"unknown mode: {req.mode}")
    if req.tier is not None and req.tier not in TIERS:
        raise RequestRejected("bad_tier", f"unknown tier: {req.tier}")
    if req.kind is not None and req.kind not in KINDS:
        raise RequestRejected("bad_kind", f"unknown job kind: {req.kind}")
    if req.review_of is not None and req.kind != "review":
        raise RequestRejected("bad_review_of", "review_of requires kind=review")
    if req.caller is not None and req.caller not in config.callers:
        raise RequestRejected("bad_caller", f"unknown caller: {req.caller}")
    if req.request_id is not None and not REQUEST_ID_PATTERN.fullmatch(req.request_id):
        raise RequestRejected(
            "bad_request_id", "request_id must be 1 to 128 characters of [A-Za-z0-9._:-]"
        )
    pool_ids = {pool.id for pool in config.pools}
    for pool in sorted(req.excluded_pools):
        if pool not in pool_ids:
            raise RequestRejected("bad_pool", f"unknown pool: {pool}")
    if req.route is not None:
        route = next((r for r in config.routes if r.id == req.route), None)
        if route is None:
            raise RequestRejected("bad_route", f"unknown route: {req.route}")
        if not route.enabled:
            raise RequestRejected("bad_route", f"route is disabled: {req.route}")
    route_ids = {route.id for route in config.routes}
    for preferred in req.preferred_routes:
        if preferred not in route_ids:
            raise RequestRejected("bad_route", f"unknown route: {preferred}")
    if req.timeout_s is not None:
        timeout = req.timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise RequestRejected("bad_timeout", "timeout_s must be an integer number of seconds")
        if not TIMEOUT_MIN_S <= timeout <= TIMEOUT_MAX_S:
            raise RequestRejected(
                "bad_timeout", f"timeout_s out of range {TIMEOUT_MIN_S}..{TIMEOUT_MAX_S}"
            )
    resolved = check_cwd(req.cwd, tasks)
    try:
        root = repo_root(resolved)
    except WorktreeError as exc:  # git failure, not "outside git": attempts would skip snapshots
        raise RequestRejected(
            "git_unavailable", f"git is not responding in the job directory: {exc}"
        ) from exc
    if req.mode == "write" and root is None:
        raise RequestRejected("not_a_git_repo", "write mode requires a directory inside git")
    return CheckedDirectory(resolved, root)


def validate_request(req: JobRequest, config: Config) -> Path | None:
    """Intake checks. Returns the git working tree root or None."""
    return validate_request_directory(req, config).repo_root
