"""Job: status transition table and request validation on intake."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_router.config import Config, TaskSettings
from agent_router.jobs import (
    STATUSES,
    TERMINAL,
    AttemptResult,
    JobRequest,
    RequestRejected,
    can_transition,
    new_job_id,
    validate_request,
)
from helpers import make_git_repo

ALLOWED = {
    ("queued", "running"),
    ("queued", "awaiting_approval"),
    ("queued", "failed"),
    ("queued", "declined"),
    ("queued", "cancelled"),
    ("awaiting_approval", "queued"),
    ("awaiting_approval", "declined"),
    ("awaiting_approval", "cancelled"),
    ("running", "completed"),
    ("running", "partial"),
    ("running", "failed"),
    ("running", "queued"),
    ("running", "cancelled"),
}


def with_roots(config: Config, allowed: list[Path], denied: list[Path] | None = None) -> Config:
    tasks = TaskSettings(allowed_roots=tuple(allowed), denied_roots=tuple(denied or ()))
    return dataclasses.replace(config, tasks=tasks)


def rejected(req: JobRequest, config: Config) -> str:
    with pytest.raises(RequestRejected) as caught:
        validate_request(req, config)
    assert caught.value.message
    return caught.value.code


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "job root"
    path.mkdir()
    return path


# --- transitions ----------------------------------------------------------------


@pytest.mark.parametrize("a", STATUSES)
@pytest.mark.parametrize("b", STATUSES)
def test_transition_table(a: str, b: str) -> None:
    assert can_transition(a, b) is ((a, b) in ALLOWED)


def test_terminal_statuses_lead_nowhere() -> None:
    assert {"completed", "partial", "failed", "declined", "cancelled"} == TERMINAL
    for a in TERMINAL:
        assert not any(can_transition(a, b) for b in STATUSES)


def test_forbidden_shortcuts() -> None:
    assert not can_transition("queued", "completed")
    assert not can_transition("awaiting_approval", "running")


def test_new_job_id_format() -> None:
    first, second = new_job_id(), new_job_id()
    assert first != second
    assert first.startswith("j_") and len(first) == 14
    int(first[2:], 16)


def test_attempt_result_defaults_and_immutability() -> None:
    result = AttemptResult("ok")
    assert (result.text, result.error, result.limit_reset_at, result.changed) == (
        "",
        None,
        None,
        None,
    )
    assert (result.head_moved, result.changes_unknown, result.survived) == (False, False, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = "error"  # type: ignore[misc]
    assert AttemptResult("ok", changed=("a",)) == AttemptResult("ok", changed=("a",))


# --- directory --------------------------------------------------------------------


def test_cwd_inside_allowed_root_is_accepted(config: Config, root: Path) -> None:
    work = root / "project one"
    work.mkdir()
    assert validate_request(JobRequest("job", work), with_roots(config, [root])) is None


def test_cwd_outside_roots(config: Config, root: Path, tmp_path: Path) -> None:
    other = tmp_path / "stranger"
    other.mkdir()
    assert rejected(JobRequest("job", other), with_roots(config, [root])) == "cwd_not_allowed"


def test_cwd_sibling_with_common_prefix_is_outside(
    config: Config, root: Path, tmp_path: Path
) -> None:
    sibling = tmp_path / "job root and more"
    sibling.mkdir()
    assert rejected(JobRequest("job", sibling), with_roots(config, [root])) == "cwd_not_allowed"


def test_cwd_inside_denied_root_nested_in_allowed(config: Config, root: Path) -> None:
    denied = root / "secrets"
    inner = denied / "deeper"
    inner.mkdir(parents=True)
    cfg = with_roots(config, [root], [denied])
    assert rejected(JobRequest("job", inner), cfg) == "cwd_not_allowed"
    assert rejected(JobRequest("job", denied), cfg) == "cwd_not_allowed"


def test_missing_cwd(config: Config, root: Path) -> None:
    assert rejected(JobRequest("job", root / "missing"), with_roots(config, [root])) == (
        "cwd_not_allowed"
    )


def test_relative_cwd(config: Config, root: Path) -> None:
    assert rejected(JobRequest("job", Path("some/dir")), with_roots(config, [root])) == (
        "cwd_not_allowed"
    )


def test_empty_allowed_roots_rejects_everything(config: Config, root: Path) -> None:
    assert rejected(JobRequest("job", root), with_roots(config, [])) == "cwd_not_allowed"


def test_escape_through_dotdot(config: Config, root: Path, tmp_path: Path) -> None:
    inner = root / "inside"
    inner.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sneaky = Path(f"{inner}{os.sep}..{os.sep}..{os.sep}outside")
    assert rejected(JobRequest("job", sneaky), with_roots(config, [root])) == "cwd_not_allowed"
    back_inside = Path(f"{inner}{os.sep}..{os.sep}inside")
    assert validate_request(JobRequest("job", back_inside), with_roots(config, [root])) is None


def _link_dir(link: Path, target: Path) -> None:
    """Directory link: a junction on Windows, a symlink on POSIX."""
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        if done.returncode != 0 or not link.exists():
            pytest.skip("failed to create junction")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("failed to create symbolic link")


def test_escape_through_link(config: Config, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    _link_dir(link, outside)
    assert rejected(JobRequest("job", link), with_roots(config, [root])) == "cwd_not_allowed"


@pytest.mark.skipif(sys.platform != "win32", reason="path case is only ignored on Windows")
def test_windows_path_in_other_case_is_accepted(config: Config, tmp_path: Path) -> None:
    root = tmp_path / "TaskRoot"
    work = root / "Project"
    work.mkdir(parents=True)
    swapped = Path(str(work).swapcase())
    assert validate_request(JobRequest("job", swapped), with_roots(config, [root])) is None


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are Windows-only")
def test_windows_unc_path_is_rejected(config: Config, root: Path) -> None:
    drive, tail = os.path.splitdrive(str(root))
    unc = Path(f"\\\\localhost\\{drive[0]}${tail}")
    cfg = with_roots(config, [root, unc])
    assert rejected(JobRequest("job", unc), cfg) == "cwd_not_allowed"
    device = Path("\\\\?\\" + str(root))
    assert rejected(JobRequest("job", device), cfg) == "cwd_not_allowed"


def test_double_slash_path_is_rejected(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    assert rejected(JobRequest("job", Path("//server/share/dir")), cfg) == "cwd_not_allowed"


# --- git --------------------------------------------------------------------------


def test_write_outside_git(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    assert rejected(JobRequest("job", root, mode="write"), cfg) == "not_a_git_repo"


def test_write_inside_git_returns_repo_root(config: Config, root: Path) -> None:
    repo = make_git_repo(root / "repository")
    sub = repo / "package"
    sub.mkdir()
    cfg = with_roots(config, [root])
    found = validate_request(JobRequest("job", sub, mode="write"), cfg)
    assert found is not None
    assert os.path.normcase(str(found.resolve())) == os.path.normcase(str(repo.resolve()))
    read = validate_request(JobRequest("job", sub), cfg)
    assert read is not None  # read jobs need the root too: changes are counted from it


@pytest.mark.parametrize("mode", ["read", "write"])
def test_git_failure_rejects_request(config: Config, root: Path, monkeypatch, mode: str) -> None:
    repo = make_git_repo(root / "repository")
    cfg = with_roots(config, [root])

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git"], 30)

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    with pytest.raises(RequestRejected) as caught:
        validate_request(JobRequest("job", repo, mode=mode), cfg)  # type: ignore[arg-type]

    assert caught.value.code == "git_unavailable"
    assert caught.value.message.startswith("git is not responding in the job directory: ")
    assert str(repo) not in caught.value.message


# --- other fields -----------------------------------------------------------------


@pytest.mark.parametrize("prompt", ["", "   \n\t"])
def test_empty_prompt(config: Config, root: Path, prompt: str) -> None:
    assert rejected(JobRequest(prompt, root), with_roots(config, [root])) == "bad_prompt"


def test_too_long_prompt(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    limit = cfg.tasks.max_prompt_chars
    assert validate_request(JobRequest("a" * limit, root), cfg) is None
    assert rejected(JobRequest("a" * (limit + 1), root), cfg) == "bad_prompt"


def test_unknown_and_disabled_route(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    assert rejected(JobRequest("job", root, route="no-such-route"), cfg) == "bad_route"
    first = next(r for r in cfg.routes if r.enabled)
    assert validate_request(JobRequest("job", root, route=first.id), cfg) is None
    off = dataclasses.replace(first, enabled=False)
    others = tuple(r for r in cfg.routes if r.id != first.id)
    cfg_off = dataclasses.replace(cfg, routes=(off, *others))
    assert rejected(JobRequest("job", root, route=first.id), cfg_off) == "bad_route"


def test_caller(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    assert rejected(JobRequest("job", root, caller="claude-cod"), cfg) == "bad_caller"
    known = next(iter(cfg.callers))
    assert validate_request(JobRequest("job", root, caller=known), cfg) is None
    assert validate_request(JobRequest("job", root), cfg) is None  # anonymous request


def test_tier_kind_mode(config: Config, root: Path) -> None:
    cfg = with_roots(config, [root])
    req = JobRequest("job", root, tier="huge")  # type: ignore[arg-type]
    assert rejected(req, cfg) == "bad_tier"
    assert rejected(JobRequest("job", root, kind="poetry"), cfg) == "bad_kind"
    req = JobRequest("job", root, mode="exec")  # type: ignore[arg-type]
    assert rejected(req, cfg) == "bad_mode"
    for kind in ("code", "review", "research", "other"):
        assert validate_request(JobRequest("job", root, kind=kind, tier="strong"), cfg) is None


@pytest.mark.parametrize("timeout", [29, 7201, 0, -5, True, 60.5])
def test_bad_timeout(config: Config, root: Path, timeout: object) -> None:
    cfg = with_roots(config, [root])
    req = JobRequest("job", root, timeout_s=timeout)  # type: ignore[arg-type]
    assert rejected(req, cfg) == "bad_timeout"


@pytest.mark.parametrize("timeout", [30, 7200, 600])
def test_good_timeout(config: Config, root: Path, timeout: int) -> None:
    cfg = with_roots(config, [root])
    assert validate_request(JobRequest("job", root, timeout_s=timeout), cfg) is None


def test_check_cwd_error_has_no_os_text(config: Config, root: Path, monkeypatch) -> None:
    cfg = with_roots(config, [root])

    def fail(*args, **kwargs):
        raise OSError(f"directory is not accessible: {root}")

    monkeypatch.setattr(Path, "resolve", fail)

    with pytest.raises(RequestRejected) as caught:
        validate_request(JobRequest("job", root), cfg)

    assert caught.value.code == "cwd_not_allowed"
    assert "OSError" in caught.value.message
    assert str(root) not in caught.value.message
    assert root.as_posix() not in caught.value.message
