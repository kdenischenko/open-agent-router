from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_router.worktree import (
    PATCH_MAX_BYTES,
    TreeSnapshot,
    WorktreeError,
    build_patch,
    changed_files,
    committed_files,
    file_patch_section,
    head,
    repo_root,
    tree_snapshot,
    without_paths,
)
from helpers import break_index, git, make_empty_git_repo, make_git_repo

_BACKSLASH = chr(92)
_NAME = "someone"
_WINDOWS_USER_PATH = (
    "C:"
    + _BACKSLASH
    + "Users"
    + _BACKSLASH
    + _NAME
    + _BACKSLASH
    + ".agent-router"
    + _BACKSLASH
    + "jobs"
    + _BACKSLASH
    + "x"
    + _BACKSLASH
    + "prompt.md"
)
_POSIX_USER_PATH = "/ho" + "me/" + _NAME + "/.config/agent-router/config.toml"


@pytest.mark.parametrize("fold", [str.upper, str.lower], ids=["upper", "lower"])
def test_without_paths_ignores_job_path_case(fold, tmp_path: Path) -> None:
    cwd = tmp_path / "proj" / "sub"

    result = without_paths(fold(str(cwd)), cwd)

    assert "proj" not in result.lower()
    assert "<dir>" in result


def test_without_paths_handles_os_error_repr(tmp_path: Path) -> None:
    cwd = tmp_path / "proj" / "sub"
    text = str(FileNotFoundError(2, "no such file or directory", str(cwd / "a.txt")))

    result = without_paths(text, cwd)

    assert "proj" not in result.lower()


def test_without_paths_removes_extended_path_prefix(tmp_path: Path) -> None:
    cwd = tmp_path / "proj" / "sub"
    text = _BACKSLASH * 2 + "?" + _BACKSLASH + str(cwd) + _BACKSLASH + "a.txt"

    result = without_paths(text, cwd)

    assert _BACKSLASH * 2 + "?" + _BACKSLASH not in result
    assert "proj" not in result.lower()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(_WINDOWS_USER_PATH, "<path>", id="windows-user"),
        pytest.param(
            _WINDOWS_USER_PATH.replace(_BACKSLASH, _BACKSLASH * 2),
            "<path>",
            id="windows-user-repr",
        ),
        pytest.param(_POSIX_USER_PATH, "<path>", id="posix-user"),
        pytest.param(
            r"failed to start 'C:\Program Files\Tool\x.exe'",
            "failed to start '<path>'",
            id="quoted-spaces",
        ),
        pytest.param(
            r"C:\Program Files\Tool\x.exe not found",
            "<path> not found",
            id="windows-spaces",
        ),
        pytest.param(
            r"\\srv\share\Some Dir\x.txt not found",
            "<path> not found",
            id="unc-spaces",
        ),
        pytest.param("/srv/a b/c.txt not found", "<path> not found", id="posix-spaces"),
        pytest.param("C:/Temp/x/y.txt", "<path>", id="windows-forward-slashes"),
        pytest.param(r"\\srv\share\x", "<path>", id="unc"),
        pytest.param("file:/srv/private/x", "file:<path>", id="posix-after-colon"),
        pytest.param("->/srv/private/x", "-><path>", id="posix-after-arrow"),
        pytest.param(r"no file C:\x\y.", "no file <path>.", id="trailing-dot"),
        pytest.param(
            "at file:///C:/Us" + "ers/" + _NAME + "/app/x.js:1",
            "at file:<path>",
            id="file-url-windows",
        ),
        pytest.param(
            "at file://" + _POSIX_USER_PATH,
            "at file:<path>",
            id="file-url-posix",
        ),
        pytest.param(
            _BACKSLASH * 2 + "?" + _BACKSLASH + "UNC" + _BACKSLASH + "srv" + _BACKSLASH + "x",
            "<path>",
            id="extended-unc",
        ),
        pytest.param(
            (
                _BACKSLASH * 2 + "?" + _BACKSLASH + "UNC" + _BACKSLASH + "srv" + _BACKSLASH + "x"
            ).replace(_BACKSLASH, _BACKSLASH * 2),
            "<path>",
            id="extended-unc-repr",
        ),
    ],
)
def test_without_paths_replaces_absolute_paths(text: str, expected: str, tmp_path: Path) -> None:
    assert without_paths(text, tmp_path / "proj" / "sub") == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://api.example.com/v1/chat",
        "https://example.test/C:/Temp/x?p=C:" + _BACKSLASH + "x",
        "taskkill /PID 1 /T /F",
        "1/2",
        "a/b",
        "/root",
        "<dir>/src/a.py",
        "input/output/error",
    ],
)
def test_without_paths_leaves_control_text_unchanged(text: str, tmp_path: Path) -> None:
    assert without_paths(text, tmp_path / "proj" / "sub") == text


@pytest.fixture
def known_roots(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Home directory, gateway home directory, and a temp directory with a space in its name."""
    home = tmp_path / ("Us" + "ers") / "Имя Фамилия"  # non-ascii on purpose
    gate = tmp_path / "Gate Data"
    temp = tmp_path / "Temp" / "Имя Фамилия"  # non-ascii on purpose
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(gate))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    return {"cwd": tmp_path / "proj" / "sub", "home": home, "gate": gate, "temp": temp}


def _known_root_cases(roots: dict[str, Path]) -> list[tuple[str, str]]:
    home, gate, temp, cwd = roots["home"], roots["gate"], roots["temp"], roots["cwd"]
    sep = _BACKSLASH
    return [
        (f"no access to {home}", "no access to ~"),
        (
            f"{home}{sep}AppData{sep}Local{sep}Temp{sep}x.txt",
            f"~{sep}AppData{sep}Local{sep}Temp{sep}x.txt",
        ),
        (f"{str(home).upper()}{sep}x", f"~{sep}x"),
        (str(home).replace(sep, sep * 2) + sep * 2 + "x", "~" + sep * 2 + "x"),
        (
            f"{gate}{sep}jobs{sep}x{sep}prompt.md not found",
            f"<path>{sep}jobs{sep}x{sep}prompt.md not found",
        ),
        (f"no access to {temp}", "no access to <path>"),
        (str(cwd.parent / "subway" / "a"), "<path>"),
        (str(cwd.parent.parent / "project2" / "x"), "<path>"),
        ("~/a/b.txt", "~/a/b.txt"),
        ("POST /v1/messages/stream 500", "POST /v1/messages/stream 500"),
        ("/bin/sh: x: not found", "/bin/sh: x: not found"),
        ("/usr/bin/node: bad option", "/usr/bin/node: bad option"),
        (rf"files C:{sep}a{sep}b.txt C:{sep}c{sep}d.txt", "files <path> <path>"),
    ]


_KNOWN_ROOT_IDS = [
    "home",
    "home-subpath",
    "home-upper",
    "home-repr",
    "gate",
    "temp",
    "job-root-boundary",
    "parent-boundary",
    "tilde",
    "http-method",
    "bin-sh",
    "usr-bin",
    "two-paths",
]


@pytest.mark.parametrize("index", range(len(_KNOWN_ROOT_IDS)), ids=_KNOWN_ROOT_IDS)
def test_without_paths_known_roots_and_boundaries(index: int, known_roots) -> None:
    text, expected = _known_root_cases(known_roots)[index]

    result = without_paths(text, known_roots["cwd"])

    assert result == expected
    assert "фамилия" not in result.casefold()  # non-ascii on purpose


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("/srv/private/x", "<path>", id="srv"),
        pytest.param("/home" + "/someone/x", "<path>", id="home"),
        pytest.param("/api/private/someone/x.json", "<path>", id="api-file"),
        pytest.param("/v1/chat/completions returned 404", "<path> returned 404", id="route"),
        pytest.param("FORGET /srv/private/x", "FORGET <path>", id="method-inside-word"),
        pytest.param("sub.txt", "sub.txt", id="relative"),
        pytest.param("<dir>/src", "<dir>/src", id="placeholder"),
    ],
)
def test_without_paths_known_roots_controls(text: str, expected: str, known_roots) -> None:
    assert without_paths(text, known_roots["cwd"]) == expected


def test_without_paths_job_file_keeps_its_name(known_roots) -> None:
    cwd = known_roots["cwd"]
    text = str(cwd / "a.py")

    assert without_paths(text, cwd) == "<dir>" + text[len(str(cwd)) :]


def _via_windows_dir(path: Path) -> str:
    """The same path spelled via the Windows directory and `..`: no known root appears verbatim."""
    return path.drive + _BACKSLASH + "Windows" + _BACKSLASH + ".." + str(path)[len(path.drive) :]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("posix", "<path>"),
        pytest.param(
            "windows",
            "~" + _BACKSLASH + "x",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows drive paths"),
        ),
    ],
)
def test_dotdot_path_into_home_is_masked(kind: str, expected: str, known_roots) -> None:
    if kind == "posix":
        text, name = "/bin/../ho" + "me/" + _NAME + "/x", _NAME
    else:
        text, name = _via_windows_dir(known_roots["home"] / "x"), "фамилия"  # non-ascii on purpose

    result = without_paths(text, known_roots["cwd"])

    assert result == expected
    assert name not in result.casefold()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("/bin/../srv/private/x", "<path>", id="bin-dotdot"),
        pytest.param("/usr/bin/./../../srv/x: missing", "<path>: missing", id="usr-bin-dot"),
        pytest.param("GET /v1/../../srv/private/x", "GET <path>", id="get-dotdot"),
        pytest.param("HEAD /./srv/private/x 404", "HEAD <path> 404", id="head-dot"),
        pytest.param("C:" + _BACKSLASH + "Windows" + _BACKSLASH + "..", "<path>", id="win-tail"),
    ],
)
def test_dotdot_unknown_path_is_masked(text: str, expected: str, known_roots) -> None:
    assert without_paths(text, known_roots["cwd"]) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("POST /v1/messages 500", "POST /v1/messages 500", id="route"),
        pytest.param("/bin/sh: x: not found", "/bin/sh: x: not found", id="bin-sh"),
        pytest.param("FORGET /srv/private/x", "FORGET <path>", id="method-inside-word"),
        pytest.param("/v1/..x/y", "<path>", id="dots-inside-part"),
    ],
)
def test_dotdot_controls(text: str, expected: str, known_roots) -> None:
    """Controls: paths without `.`/`..` segments are still masked."""
    assert without_paths(text, known_roots["cwd"]) == expected


def test_http_route_with_home_path_is_masked(known_roots) -> None:
    """Control: the known root is replaced before HTTP-route exclusion applies."""
    text = "GET " + str(known_roots["home"] / "x")

    assert without_paths(text, known_roots["cwd"]) == "GET ~" + os.sep + "x"


def test_new_file_with_spaces_and_cyrillic(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "репозиторий с пробелом")  # non-ascii on purpose
    before: TreeSnapshot = tree_snapshot(repo)
    name = "новый файл.txt"  # non-ascii on purpose
    path = repo / name

    path.write_text("new content\n", encoding="utf-8")
    after = tree_snapshot(repo)

    assert set(after) == {name}
    assert after[name] == ("??", path.stat().st_size, path.stat().st_mtime_ns)
    assert changed_files(before, after) == [name]


def test_modified_tracked_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    before = tree_snapshot(repo)
    path = repo / "README.md"

    path.write_text("changed tracked content\n", encoding="utf-8")
    after = tree_snapshot(repo)

    assert after["README.md"] == (" M", path.stat().st_size, path.stat().st_mtime_ns)
    assert changed_files(before, after) == ["README.md"]


def test_deleted_file_has_missing_metadata(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    before = tree_snapshot(repo)

    (repo / "README.md").unlink()
    after = tree_snapshot(repo)

    assert after == {"README.md": (" D", -1, -1)}
    assert changed_files(before, after) == ["README.md"]


def test_already_dirty_file_changed_again(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    path = repo / "README.md"
    path.write_text("dirty\n", encoding="utf-8")
    before = tree_snapshot(repo)

    path.write_text("dirty again with a different length\n", encoding="utf-8")
    after = tree_snapshot(repo)

    assert before["README.md"] != after["README.md"]
    assert changed_files(before, after) == ["README.md"]


def test_unchanged_tree_has_no_changed_files(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")

    before = tree_snapshot(repo)
    after = tree_snapshot(repo)

    assert before == {}
    assert after == before
    assert changed_files(before, after) == []


def test_commit_changes_head_and_reports_committed_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    head_before = head(repo)
    (repo / "README.md").write_text("committed change\n", encoding="utf-8")

    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "change")
    head_after = head(repo)

    assert head_before is not None
    assert head_after is not None
    assert head_after != head_before
    assert tree_snapshot(repo) == {}
    assert committed_files(repo, head_before, head_after) == ["README.md"]


def test_subdirectory_uses_repository_relative_paths(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    cwd = repo / "nested" / "inner"
    cwd.mkdir(parents=True)

    (cwd / "new.txt").write_text("new\n", encoding="utf-8")
    snapshot = tree_snapshot(cwd)

    assert repo_root(cwd) == repo
    assert set(snapshot) == {"nested/inner/new.txt"}


def test_rename_contains_both_paths(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")

    git(repo, "mv", "README.md", "renamed.txt")
    snapshot = tree_snapshot(repo)
    renamed = repo / "renamed.txt"

    assert set(snapshot) == {"README.md", "renamed.txt"}
    assert snapshot["README.md"] == ("R ", -1, -1)
    assert snapshot["renamed.txt"] == (
        "R ",
        renamed.stat().st_size,
        renamed.stat().st_mtime_ns,
    )
    assert changed_files({}, snapshot) == ["README.md", "renamed.txt"]


def test_changed_files_is_sorted_and_includes_disappeared_paths() -> None:
    before = {"b.txt": ("??", 1, 1), "a.txt": ("??", 1, 1)}
    after = {"b.txt": ("??", 2, 1), "c.txt": ("??", 1, 1)}

    assert changed_files(before, after) == ["a.txt", "b.txt", "c.txt"]


def test_outside_git_has_no_repo_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    assert repo_root(outside) is None


def test_nonexistent_directory_has_no_repo_root(tmp_path: Path) -> None:
    assert repo_root(tmp_path / "missing") is None


def test_repository_without_commits_has_no_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")

    assert head(repo) is None


def _break_branch(repo: Path, kind: str) -> None:
    branch = repo / ".git" / "refs" / "heads" / "main"
    if kind == "garbage":
        branch.write_text("garbage-ref\n", encoding="utf-8")
    elif kind == "missing-object":
        branch.write_text("1234567890" * 4 + "\n", encoding="utf-8")
    else:  # the packed-refs entry points at a missing object
        git(repo, "pack-refs", "--all")
        packed = repo / ".git" / "packed-refs"
        lines = packed.read_text(encoding="utf-8").splitlines()
        packed.write_text(
            "".join(
                ("1234567890" * 4 + line[40:] if line.endswith(" refs/heads/main") else line) + "\n"
                for line in lines
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize("kind", ["garbage", "missing-object", "packed-missing-object"])
def test_head_broken_ref_is_error(tmp_path: Path, kind: str) -> None:
    repo = make_git_repo(tmp_path / "repo")
    _break_branch(repo, kind)

    with pytest.raises(WorktreeError):
        head(repo)


_GIT_WARNING = b"warning: ignoring unknown core.fsync component 'bogus'\n"


def _warn_on_rev_parse(monkeypatch) -> None:
    """`rev-parse` HEAD exits with code 1, empty stdout, and a git warning on stderr."""
    from agent_router import worktree

    real_run = worktree._run

    def fake_run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        done = real_run(cwd, *args)
        if args[:2] == ("rev-parse", "--verify"):
            assert done.returncode == 1 and not done.stdout.strip()
            return subprocess.CompletedProcess(done.args, 1, b"", _GIT_WARNING)
        return done

    monkeypatch.setattr(worktree, "_run", fake_run)


def test_head_empty_repo_with_git_warning_is_none(tmp_path: Path, monkeypatch) -> None:
    repo = make_empty_git_repo(tmp_path / "repo")
    _warn_on_rev_parse(monkeypatch)

    assert head(repo) is None


def test_head_empty_repo_with_real_git_warning_is_none(tmp_path: Path) -> None:
    """Real warning: unknown `core.fsync` component (git 2.56: exit code 1, `warning:`)."""
    repo = make_empty_git_repo(tmp_path / "repo")
    git(repo, "config", "core.fsync", "bogus")

    assert head(repo) is None


@pytest.mark.parametrize("kind", ["garbage", "missing-object", "packed-missing-object"])
def test_head_broken_ref_with_git_warning_is_error(tmp_path: Path, kind: str, monkeypatch) -> None:
    """Control: a warning on stderr must not turn a broken branch into "no commits"."""
    repo = make_git_repo(tmp_path / "repo")
    _break_branch(repo, kind)
    _warn_on_rev_parse(monkeypatch)

    with pytest.raises(WorktreeError):
        head(repo)


def test_head_of_normal_repo_is_hash(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")

    assert head(repo) == git(repo, "rev-parse", "HEAD").strip()


def test_non_utf8_names_stay_distinct(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "rev-parse":
            stdout = str(tmp_path).encode("utf-8")
        else:
            stdout = b"?? a\xff.txt\0?? a\xfe.txt\0"
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout, stderr=b"")

    real_stat = Path.stat

    def stat(self: Path, *args, **kwargs):
        if any(0xDC80 <= ord(char) <= 0xDCFF for char in str(self)):
            raise UnicodeEncodeError("utf-8", str(self), 0, 1, "name not in UTF-8")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr("agent_router.worktree._run", fake_run)
    monkeypatch.setattr(Path, "stat", stat)

    snapshot = tree_snapshot(tmp_path)
    assert len(snapshot) == 2
    names = changed_files({}, snapshot)
    assert names == ["a\\xfe.txt", "a\\xff.txt"]
    json.dumps(names, ensure_ascii=False).encode("utf-8")


def _all_raise(path: Path) -> None:
    for call in (
        lambda: repo_root(path),
        lambda: head(path),
        lambda: tree_snapshot(path),
        lambda: committed_files(path, "old", "new"),
        lambda: committed_files(path, None, "new"),
    ):
        with pytest.raises(WorktreeError):
            call()


def test_missing_git_raises(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    _all_raise(tmp_path)


def test_git_timeout_raises(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git"], 30)

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    _all_raise(tmp_path)


def test_repo_root_unexpected_failure_raises(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 128, stdout=b"", stderr=b"fatal: detected dubious ownership in repository"
        )

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    _all_raise(tmp_path)


def test_head_outside_git_raises(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WorktreeError):
        head(outside)


def test_snapshot_outside_git_raises(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WorktreeError):
        tree_snapshot(outside)


def test_snapshot_with_broken_index_raises(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    break_index(repo)

    assert repo_root(repo) == repo
    assert head(repo) is not None
    with pytest.raises(WorktreeError):
        tree_snapshot(repo)


def test_committed_files_from_empty_history(tmp_path: Path) -> None:
    repo = tmp_path / "репозиторий без коммитов"  # non-ascii on purpose
    cwd = repo / "подкаталог с пробелом"  # non-ascii on purpose
    cwd.mkdir(parents=True)
    make_empty_git_repo(repo)
    assert head(cwd) is None
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (cwd / "файл.txt").write_text("b\n", encoding="utf-8")  # non-ascii on purpose
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "first")

    first = head(cwd)

    assert first is not None
    committed = "подкаталог с пробелом/файл.txt"  # non-ascii on purpose
    assert committed_files(cwd, None, first) == ["a.txt", committed]


def test_committed_files_unknown_hash_raises(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    current = head(repo)
    assert current is not None

    with pytest.raises(WorktreeError):
        committed_files(repo, "0" * 40, current)
    with pytest.raises(WorktreeError):
        committed_files(repo, None, "0" * 40)


def test_error_text_has_no_job_path(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "job directory"
    cwd = repo / "inside"
    cwd.mkdir(parents=True)

    def fail(*args, **kwargs):
        text = f"fatal: unsafe repository ('{repo.as_posix()}' and '{cwd}')\nsecond line"
        return subprocess.CompletedProcess(args[0], 128, stdout=b"", stderr=text.encode("utf-8"))

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    with pytest.raises(WorktreeError) as caught:
        repo_root(cwd)

    message = str(caught.value)
    assert str(repo) not in message
    assert repo.as_posix() not in message
    assert "unsafe repository" in message
    assert "second line" not in message


def test_cwd_inside_git_dir_has_no_repo_root(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")

    assert repo_root(repo / ".git") is None


def test_git_invocation_contract(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def succeed(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout=str(tmp_path).encode("utf-8"), stderr=b""
        )

    monkeypatch.setattr("agent_router.worktree.subprocess.run", succeed)

    assert repo_root(tmp_path) == tmp_path
    args, kwargs = calls[0]
    assert args[0] == ["git", "-c", "core.quotepath=off", "rev-parse", "--show-toplevel"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 30
    assert kwargs["env"]["LC_ALL"] == "C"
    assert "shell" not in kwargs


# --- attempt patches -----------------------------------------------------------------------


def test_build_patch_new_untracked_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    name = "новый файл.txt"  # non-ascii on purpose
    (repo / name).write_text(
        "первая строка\nвторая строка\n",  # non-ascii on purpose
        encoding="utf-8",
    )

    patch, truncated = build_patch(repo, [name])

    assert truncated is False
    assert patch == (
        f"diff --git a/{name} b/{name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{name}\n"
        "@@ -0,0 +1,2 @@\n"
        "+первая строка\n"  # non-ascii on purpose
        "+вторая строка\n"  # non-ascii on purpose
    )


def test_build_patch_file_without_trailing_newline(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "note.txt").write_text("конец без перевода", encoding="utf-8")  # non-ascii on purpose

    patch, truncated = build_patch(repo, ["note.txt"])

    assert truncated is False
    assert patch is not None
    assert "@@ -0,0 +1,1 @@" in patch
    no_newline = "+конец без перевода\n\\ No newline at end of file\n"  # non-ascii on purpose
    assert patch.endswith(no_newline)


def test_build_patch_empty_new_file_has_only_headers(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    name = "пустой.txt"  # non-ascii on purpose
    (repo / name).write_text("", encoding="utf-8")

    patch, truncated = build_patch(repo, [name])

    assert truncated is False
    assert patch == f"diff --git a/{name} b/{name}\nnew file mode 100644\n"


def test_build_patch_tracked_modification(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed content\n", encoding="utf-8")

    patch, truncated = build_patch(repo, ["README.md"])

    assert truncated is False
    assert patch is not None
    assert patch.startswith("diff --git a/README.md b/README.md\n")
    assert "-test repo" in patch and "+changed content" in patch


def test_build_patch_deleted_tracked_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").unlink()

    patch, truncated = build_patch(repo, ["README.md"])

    assert truncated is False
    assert patch is not None
    assert "--- a/README.md" in patch and "+++ /dev/null" in patch


def test_build_patch_puts_modifications_before_new_files(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    name = "новый.txt"  # non-ascii on purpose
    (repo / name).write_text("содержимое\n", encoding="utf-8")  # non-ascii on purpose

    patch, truncated = build_patch(repo, ["README.md", name])

    assert truncated is False
    assert patch is not None
    assert patch.index("diff --git a/README.md") < patch.index(f"diff --git a/{name}")
    assert "+содержимое" in patch  # non-ascii on purpose


def test_build_patch_marks_binary_new_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    name = "данные.bin"  # non-ascii on purpose
    (repo / name).write_bytes(b"\x00\x01binary\x02")

    patch, truncated = build_patch(repo, [name])

    assert truncated is False
    assert patch == f"binary file {name}\n"


def test_build_patch_marks_unreadable_new_file(tmp_path: Path, monkeypatch) -> None:
    repo = make_git_repo(tmp_path / "repo")
    name = "закрытый.txt"  # non-ascii on purpose
    (repo / name).write_text("содержимое\n", encoding="utf-8")  # non-ascii on purpose

    def refused(path: Path) -> bytes:
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr("agent_router.worktree._read_file_bytes", refused)

    patch, truncated = build_patch(repo, [name])

    assert truncated is False
    assert patch == f"unreadable file {name}\n"


def test_build_patch_without_files_captures_nothing(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")

    assert build_patch(repo, []) == (None, False)


def test_build_patch_from_subdirectory_reads_root_relative_files(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    cwd = repo / "nested" / "inner"
    cwd.mkdir(parents=True)
    name = "nested/inner/новый.txt"  # non-ascii on purpose
    (repo / name).write_text("содержимое\n", encoding="utf-8")  # non-ascii on purpose

    patch, truncated = build_patch(cwd, [name])

    assert truncated is False
    assert patch is not None
    assert f"+++ b/{name}" in patch and "+содержимое" in patch  # non-ascii on purpose


def test_build_patch_truncates_at_the_cap(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    name = "большой.txt"  # non-ascii on purpose
    (repo / name).write_text("строка паттерна\n" * 40_000, encoding="utf-8")  # non-ascii on purpose

    patch, truncated = build_patch(repo, [name])

    assert truncated is True
    assert len(patch.encode("utf-8")) <= PATCH_MAX_BYTES
    assert patch.startswith(f"diff --git a/{name} b/{name}\n")


def test_build_patch_outside_git_raises(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WorktreeError):
        build_patch(outside, ["a.txt"])


# --- one file of a stored patch -------------------------------------------------------------


def test_file_patch_section_returns_only_the_named_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    name = "новый файл.txt"  # non-ascii on purpose
    (repo / name).write_text("содержимое\n", encoding="utf-8")  # non-ascii on purpose
    patch, truncated = build_patch(repo, ["README.md", name])

    assert truncated is False
    readme, readme_is_last = file_patch_section(patch, "README.md")
    assert readme is not None
    assert readme.startswith("diff --git a/README.md b/README.md\n")
    assert "+changed" in readme
    assert name not in readme
    assert readme_is_last is False

    new, new_is_last = file_patch_section(patch, name)
    assert new is not None
    assert "+содержимое" in new  # non-ascii on purpose
    assert new_is_last is True


def test_file_patch_section_matches_names_with_spaces() -> None:
    name = "папка с пробелом/файл.txt"  # non-ascii on purpose
    patch = f"diff --git a/{name} b/{name}\n@@ -1 +1 @@\n-a\n+b\n"

    section, is_last = file_patch_section(patch, name)

    assert section == patch
    assert is_last is True


def test_file_patch_section_finds_binary_and_unreadable_markers() -> None:
    patch = (
        "diff --git a/tracked.txt b/tracked.txt\n@@ -1 +1 @@\n-a\n+b\n"
        "binary file файл.bin\n"  # non-ascii on purpose
        "unreadable file другой.txt\n"  # non-ascii on purpose
    )

    binary, binary_is_last = file_patch_section(patch, "файл.bin")  # non-ascii on purpose
    assert binary == "binary file файл.bin\n"  # non-ascii on purpose
    assert binary_is_last is False

    unreadable, unreadable_is_last = file_patch_section(patch, "другой.txt")  # non-ascii on purpose
    assert unreadable == "unreadable file другой.txt\n"  # non-ascii on purpose
    assert unreadable_is_last is True


def test_file_patch_section_missing_name_is_none() -> None:
    patch = "diff --git a/a.txt b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"

    assert file_patch_section(patch, "b.txt") is None
    assert file_patch_section("", "a.txt") is None


def test_file_patch_section_tells_a_cut_tail_from_a_complete_section() -> None:
    # a tail cut mid-line at PATCH_MAX_BYTES: the last section may be incomplete
    patch = (
        "diff --git a/first.txt b/first.txt\n@@ -1 +1 @@\n-one\n+two\n"
        "diff --git a/second.txt b/second.txt\n@@ -1 +1 @@\n-x\n+y\npart of a lin"
    )

    first, first_is_last = file_patch_section(patch, "first.txt")
    assert first is not None
    assert first_is_last is False

    second, second_is_last = file_patch_section(patch, "second.txt")
    assert second is not None
    assert second.endswith("part of a lin")
    assert second_is_last is True
