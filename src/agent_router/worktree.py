"""Git working tree snapshot before and after an attempt: read-only git commands."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from agent_router.config import home_dir

TreeSnapshot = dict[str, tuple[str, int, int]]

_TIMEOUT_S = 30
# a stored patch never exceeds this many bytes; a longer one is cut and flagged
PATCH_MAX_BYTES = 256 * 1024
# git replies that reliably mean "not in a working tree" (text as printed with LC_ALL=C)
_NOT_A_WORKTREE = ("not a git repository", "must be run in a work tree")
# a `file://` address is an on-disk path: we do not protect it, the path rule will replace it
_URL_RE = re.compile(r"(?<![A-Za-z0-9+.-])(?!(?i:file)://)[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+")
_ABSOLUTE_PATH_RE = re.compile(
    r"""
    (?:
        (?<!\w)\\{2,4}\?\\{1,2}(?i:unc)\\{1,2}[^\s"'<>|]*
        |
        (?<!\w)(?:\\{2,4}\?\\{1,2})?[A-Za-z]:[\\/]{1,2}[^\s"'<>|]*
        |
        \\{2,4}(?![?\\])[^\s"'<>|]*
        |
        (?<![\w/.])/[^\s"'<>|]*
    )
    """,
    re.VERBOSE,
)
_HTTP_METHOD_RE = re.compile(r"(?<!\w)(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) $")
_HTTP_METHOD_MAX = len("OPTIONS ") + 1  # tail before the path, scanned for an HTTP method verb
_EXTENDED_PREFIX = r"(?:\\{2,4}\?\\{1,2})?"
_ROOT_BOUNDARY = r"(?=[\\/\s\"'<>|]|$|[.,:;)](?![^\W_]))"
_PARENT_ROOT_BOUNDARY = r"(?=[\s\"'<>|]|$|[.,:;)](?![^\W_]))"
_PATH_PLACEHOLDERS = ("~", "<dir>", "<path>")
_PROTECTED_POSIX_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/")
_TRAILING_PUNCTUATION = ".,:;)"
_DOT_PART_RE = re.compile(r"[\\/]\.{1,2}(?=[\\/]|$)")
_DOT_TAIL_RE = re.compile(r"(\.|\.\.)[,:;)]*")


class WorktreeError(Exception):
    """git failed to start, exceeded its deadline, or returned an error: tree state unknown."""


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_S,
            env=dict(os.environ, LC_ALL="C"),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {args[0]}: no response within {_TIMEOUT_S} s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        # the OS exception text may contain the job directory path — only the type is kept
        raise WorktreeError(f"git {args[0]}: failed to start ({type(exc).__name__})") from exc


def _path_end(text: str, start: int, end: int) -> int:
    if start and text[start - 1] in "'\"":
        quote = text[start - 1]
        closing = text.find(quote, end)
        if closing >= 0:
            return closing
    can_continue = not text[start:end].startswith("/") or text[start:end].count("/") >= 2
    while can_continue and end < len(text) and text[end] == " ":
        word_start = end + 1
        word_end = word_start
        while (
            word_end < len(text) and not text[word_end].isspace() and text[word_end] not in "'\"<>|"
        ):
            word_end += 1
        if _ABSOLUTE_PATH_RE.match(text, word_start) or not {"\\", "/"}.intersection(
            text[word_start:word_end]
        ):
            break
        end = word_end
    return end


def _normalized_root_replacement(path: str, folders: list[tuple[Path, str, bool]]) -> str | None:
    for folder, replacement, is_parent in folders:
        variants = dict.fromkeys(
            (str(folder), folder.as_posix(), str(folder).replace("\\", "\\\\"))
        )
        for known in variants:
            boundary = "$" if is_parent else r"(?=$|[\\/])"
            pattern = re.compile(
                "^" + _EXTENDED_PREFIX + re.escape(known) + boundary, re.IGNORECASE
            )
            if match := pattern.match(path):
                return replacement if is_parent else replacement + path[match.end() :]
    return None


def _replace_absolute_paths(
    text: str, folders: list[tuple[Path, str, bool]], *, dotted_only: bool = False
) -> str:
    result = []
    cursor = 0
    search_from = 0
    while match := _ABSOLUTE_PATH_RE.search(text, search_from):
        start = match.start()
        matched = match.group()
        if text[:start].endswith(_PATH_PLACEHOLDERS):
            search_from = match.end()
            continue
        end = _path_end(text, start, match.end())
        path = text[start:end].rstrip(_TRAILING_PUNCTUATION)
        trailing = text[start + len(path) : end]
        dot_tail = _DOT_TAIL_RE.fullmatch(trailing)
        if dot_tail and path.endswith(("/", "\\")):
            path += dot_tail.group(1)
        end = start + len(path)
        has_dot_parts = _DOT_PART_RE.search(path) is not None
        if has_dot_parts:
            normalize = posixpath.normpath if path.startswith("/") else ntpath.normpath
            normalized = normalize(path)
            replacement = _normalized_root_replacement(normalized, folders) or "<path>"
        else:
            if dotted_only:
                search_from = end
                continue
            if matched.startswith("/") and (
                _HTTP_METHOD_RE.search(text[max(0, start - _HTTP_METHOD_MAX) : start])
                or matched.startswith(_PROTECTED_POSIX_PREFIXES)
            ):
                search_from = match.end()
                continue
            if path.startswith("/") and path.count("/") < 2:
                search_from = match.end()
                continue
            replacement = "<path>"
        result.extend((text[cursor:start], replacement))
        cursor = end
        search_from = end
    result.append(text[cursor:])
    return "".join(result)


def _without_paths_in_segment(text: str, folders: list[tuple[Path, str, bool]]) -> str:
    text = _replace_absolute_paths(text, folders, dotted_only=True)
    for folder, replacement, is_parent in folders:
        variants = dict.fromkeys(
            (str(folder), folder.as_posix(), str(folder).replace("\\", "\\\\"))
        )
        for known in variants:
            boundary = _PARENT_ROOT_BOUNDARY if is_parent else _ROOT_BOUNDARY
            pattern = re.compile(_EXTENDED_PREFIX + re.escape(known) + boundary, re.IGNORECASE)
            text = pattern.sub(replacement, text)
    return _replace_absolute_paths(text, folders)


def _raw_and_resolved(root: Path) -> tuple[Path, ...]:
    try:
        return root, root.resolve()
    except (OSError, RuntimeError):
        return (root,)


def without_paths(text: str, cwd: Path) -> str:
    """Replaces known roots and remaining absolute paths, keeping URL addresses."""
    roots = _raw_and_resolved(cwd)
    folder_kinds: dict[Path, tuple[str, bool]] = {}
    for root in roots:
        folder_kinds[root] = ("<dir>", False)
        for parent in root.parents:
            folder_kinds.setdefault(parent, ("<dir>", True))

    known_roots = [(home_dir(), "<path>")]
    for name in ("TEMP", "TMP", "TMPDIR"):
        if value := os.environ.get(name):
            known_roots.append((Path(value), "<path>"))
    try:
        home = Path.home()
    except (RuntimeError, KeyError, OSError):
        home = None
    if home is not None:
        known_roots.append((home, "~"))
    for known_root, replacement in known_roots:
        for root in _raw_and_resolved(known_root):
            if root not in roots:
                folder_kinds[root] = (replacement, False)

    folders = sorted(
        (
            (folder, replacement, is_parent)
            for folder, (replacement, is_parent) in folder_kinds.items()
            if len(folder.parts) > 1
        ),
        key=lambda item: (len(str(item[0])), len(item[0].parts)),
        reverse=True,
    )

    result = []
    cursor = 0
    for match in _URL_RE.finditer(text):
        result.append(_without_paths_in_segment(text[cursor : match.start()], folders))
        result.append(match.group())
        cursor = match.end()
    result.append(_without_paths_in_segment(text[cursor:], folders))
    return "".join(result)


def _failure(cwd: Path, command: str, done: subprocess.CompletedProcess[bytes]) -> WorktreeError:
    """git error: command, exit code, and first stderr line with the job directory path removed."""
    lines = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    detail = without_paths(lines[0], cwd) if lines else "no message"
    return WorktreeError(f"git {command}: exit code {done.returncode}: {detail}")


def _git(cwd: Path, *args: str, errors: str = "replace") -> str:
    done = _run(cwd, *args)
    if done.returncode != 0:
        raise _failure(cwd, args[0], done)
    return done.stdout.decode("utf-8", errors)


def _shown_name(name: str) -> str:
    """Name from git output, for the report: non-UTF-8 bytes are escaped as `\\xff`,
    without surrogates."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def repo_root(cwd: Path) -> Path | None:
    """Working tree root; None only when "outside git" is certain, a git failure raises
    WorktreeError."""
    if not cwd.is_dir():
        return None
    done = _run(cwd, "rev-parse", "--show-toplevel")
    if done.returncode == 0:
        output = done.stdout.decode("utf-8", "replace").rstrip("\r\n")
        if not output:
            raise WorktreeError("git rev-parse: empty response")
        return Path(output)
    message = (done.stderr or b"").decode("utf-8", "replace").lower()
    if done.returncode == 128 and any(mark in message for mark in _NOT_A_WORKTREE):
        return None
    raise _failure(cwd, "rev-parse", done)


def _require_root(cwd: Path, command: str) -> Path:
    root = repo_root(cwd)
    if root is None:
        raise WorktreeError(f"git {command}: directory outside a git working tree")
    return root


def _git_path(cwd: Path, name: str) -> Path:
    path = Path(_git(cwd, "rev-parse", "--git-path", name).rstrip("\r\n"))
    return path if path.is_absolute() else cwd / path


def _branch_is_unborn(cwd: Path) -> bool:
    """HEAD points at a branch that exists neither as a file nor as a line in packed-refs."""
    done = _run(cwd, "symbolic-ref", "-q", "HEAD")
    ref = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not ref:
        return False
    if _git_path(cwd, ref).exists():
        return False
    try:
        packed = _git_path(cwd, "packed-refs").read_bytes().decode("utf-8", "replace")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not any(line.endswith(" " + ref) for line in packed.splitlines())


def head(cwd: Path) -> str | None:
    """HEAD hash; None only for "no commits yet", a git failure or directory outside git raises
    WorktreeError."""
    done = _run(cwd, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    output = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode == 0 and output:
        return output
    # with -q git prints no error at exit code 1 (a real failure is exit code 128), but warnings
    # (`warning: ...` about settings) stay in stderr: the branch check decides, not stderr
    if done.returncode == 1 and not output:
        if _branch_is_unborn(cwd):
            return None
        raise WorktreeError("git rev-parse: HEAD ref corrupted or does not point at a commit")
    raise _failure(cwd, "rev-parse", done)


def tree_snapshot(cwd: Path) -> TreeSnapshot:
    """State of modified and new files; a git failure or directory outside git raises
    WorktreeError."""
    root = _require_root(cwd, "status")
    # surrogateescape: distinct non-UTF-8 filename bytes stay distinct snapshot keys
    output = _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", errors="surrogateescape"
    )

    snapshot: TreeSnapshot = {}
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        paths = [record[3:]]
        if ("R" in status or "C" in status) and index < len(records):
            paths.append(records[index])
            index += 1
        for relative in paths:
            try:
                stat = (root / relative).stat()
                size, mtime_ns = stat.st_size, stat.st_mtime_ns
            except (OSError, UnicodeError):
                size, mtime_ns = -1, -1
            snapshot[relative] = (status, size, mtime_ns)
    return snapshot


def changed_files(before: TreeSnapshot, after: TreeSnapshot) -> list[str]:
    return sorted(
        _shown_name(path)
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def committed_files(cwd: Path, head_before: str | None, head_after: str) -> list[str]:
    """Files of the commits between two HEADs; without `head_before`, the whole first-commit
    tree."""
    if head_before is None:
        root = _require_root(cwd, "ls-tree")
        args = ("ls-tree", "-r", "--name-only", "-z", "--full-tree", head_after)
    else:
        root = _require_root(cwd, "diff")
        args = ("diff", "--name-only", "-z", head_before, head_after)
    output = _git(root, *args, errors="surrogateescape")
    return [_shown_name(path) for path in output.split("\0") if path]


# --- attempt patch ---------------------------------------------------------------------------


def _literal(path: str) -> str:
    """A pathspec that matches the path itself, never a glob pattern inside it."""
    return f":(literal){path}"


def _status_records(root: Path, files: Sequence[str]) -> dict[str, tuple[str, str]]:
    """Status of exactly the given files: shown name → (status, raw path).

    Files missing from the output are clean, gone, or renamed; they are treated as
    tracked and left to `git diff`.
    """
    if not files:
        return {}
    output = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *(_literal(name) for name in files),
        errors="surrogateescape",
    )
    records: dict[str, tuple[str, str]] = {}
    parts = output.split("\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        raw = record[3:]
        if ("R" in status or "C" in status) and index < len(parts):
            index += 1  # the original path of a rename or copy is not the changed file
        records[_shown_name(raw)] = (status, raw)
    return records


def _read_file_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _new_file_patch(name: str, data: bytes) -> str:
    """A synthetic new-file unified diff built from the file content, without git.

    Binary content becomes a one-line marker instead of raw bytes.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"binary file {name}\n"
    if "\x00" in text:
        return f"binary file {name}\n"
    lines = text.split("\n")
    if lines == [""]:
        return f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
    ends_with_newline = lines[-1] == ""
    shown = lines[:-1] if ends_with_newline else lines
    body = "".join(f"+{line}\n" for line in shown)
    if not ends_with_newline:
        body += "\\ No newline at end of file\n"
    return (
        f"diff --git a/{name} b/{name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{name}\n"
        f"@@ -0,0 +1,{len(shown)} @@\n"
        f"{body}"
    )


def _tracked_diff(root: Path, paths: Sequence[str]) -> str:
    """`git diff` of the given tracked paths against HEAD, or against the index in a
    repository without commits. The index is only ever read."""
    if not paths:
        return ""
    base = ("HEAD",) if head(root) is not None else ()
    return _git(root, "diff", *base, "--", *(_literal(path) for path in paths))


def build_patch(cwd: Path, files: Sequence[str]) -> tuple[str | None, bool]:
    """Unified diff of exactly `files`, for the attempt that changed them.

    Tracked modifications come from `git diff`; untracked new files get a synthetic
    new-file diff built from their content; binary or unreadable files become one-line
    markers. The git index is never touched. Returns the patch and whether it was cut
    at PATCH_MAX_BYTES; None when there is nothing to show.
    """
    if not files:
        return None, False
    root = _require_root(cwd, "diff")
    records = _status_records(root, files)
    tracked: list[str] = []
    sections: list[str] = []
    for name in files:
        record = records.get(name)
        if record is not None and record[0] == "??":
            try:
                data = _read_file_bytes(root / record[1])
            except OSError:
                sections.append(f"unreadable file {name}\n")
                continue
            sections.append(_new_file_patch(name, data))
        else:
            # not in the status output, or tracked: the raw name is unknown only for
            # non-UTF-8 paths, where the shown name is the best available pathspec
            tracked.append(record[1] if record is not None else name)
    patch = _tracked_diff(root, tracked) + "".join(sections)
    encoded = patch.encode("utf-8")
    if not encoded:
        return None, False
    if len(encoded) <= PATCH_MAX_BYTES:
        return patch, False
    return encoded[:PATCH_MAX_BYTES].decode("utf-8", "ignore"), True


def file_patch_section(patch: str, name: str) -> tuple[str, bool] | None:
    """The part of a stored patch that belongs to one file, and whether it ends the patch.

    build_patch output is a run of sections: a `diff --git a/<name> b/<name>` header
    for each file diff and one-line `binary file`/`unreadable file` markers. The
    second value says the section is the patch's last: the tail of a patch cut at
    PATCH_MAX_BYTES may be incomplete mid-file. None when the file has no section;
    a rename, whose header names two different paths, is left to a live diff.
    """
    headers = (
        f"diff --git a/{name} b/{name}\n",
        f"binary file {name}\n",
        f"unreadable file {name}\n",
    )
    section: list[str] = []
    found = False
    for line in patch.splitlines(keepends=True):
        boundary = line.startswith("diff --git ") or line.startswith(
            ("binary file ", "unreadable file ")
        )
        if boundary:
            if found:
                return "".join(section), False
            found = line in headers
            section = []
        if found:
            section.append(line)
    if not found:
        return None
    return "".join(section), True
