"""Repository hygiene: nothing personal or secret in the public files."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAILS = {"noreply@anthropic.com", "git@github.com"}
PUBLIC_DIRS = ("src/", "tests/", "integrations/", "docs/", ".github/")
PUBLIC_ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "ROADMAP.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
}
USER_PATHS = [
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!<)[^\\/\s<>\"'`*|)]+", re.IGNORECASE),
    re.compile(r"/home/(?!<)[A-Za-z0-9._-]+"),
    re.compile(r"/Users/(?!<)[A-Za-z0-9._-]+"),
]
TOKENS = [
    re.compile(r"\d{8,10}:AA[\w-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
]
SESSION_IDS = [
    re.compile(r"session_[0-9a-f]{8}-[0-9a-f]{4}-"),
    re.compile(r"ses_[0-9A-Za-z]{20,}"),
]


def is_public(name: str) -> bool:
    """Files that belong to the project; local working files are not checked."""
    return name.startswith(PUBLIC_DIRS) or name in PUBLIC_ROOT_FILES


def tracked_texts() -> list[tuple[str, str]]:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=off", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )
    texts = []
    for name in done.stdout.decode("utf-8").split("\0"):
        if not name or not is_public(name):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            texts.append((name, path.read_bytes().decode("utf-8")))
        except UnicodeDecodeError:
            continue  # binary file
    return texts


@pytest.fixture(scope="module")
def texts() -> list[tuple[str, str]]:
    if shutil.which("git") is None:
        pytest.skip("git is required to list tracked files")
    found = tracked_texts()
    assert found, "git ls-files returned nothing"
    return found


def find(texts: list[tuple[str, str]], patterns: list[re.Pattern[str]]) -> list[str]:
    hits = []
    for name, text in texts:
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{name}:{line}: {match.group(0)[:12]}…")
    return hits


def test_no_emails(texts):
    hits = []
    for name, text in texts:
        for match in EMAIL.finditer(text):
            address = match.group(0).lower()
            if address in ALLOWED_EMAILS or address.endswith("@example.com"):
                continue
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{name}:{line}")
    assert hits == []


def test_no_user_paths(texts):
    assert find(texts, USER_PATHS) == []


def test_no_tokens(texts):
    assert find(texts, TOKENS) == []


def test_no_session_ids(texts):
    assert find(texts, SESSION_IDS) == []


def test_patterns_catch_samples():
    """The patterns themselves work: they catch samples assembled piece by piece."""
    assert EMAIL.search("some" + "body@" + "mail.example.org")
    sep = chr(92)
    assert USER_PATHS[0].search("C:" + sep + "Users" + sep + "someone" + sep + "x")
    assert USER_PATHS[0].search("c:/" + "users/" + "someone/x")
    assert not USER_PATHS[0].search("C:" + sep + "Users" + sep + "<name>" + sep + "x")
    assert USER_PATHS[1].search("/ho" + "me/someone/x")
    assert TOKENS[0].search("123456789" + ":AA" + "b" * 32)
    assert TOKENS[1].search("sk" + "-" + "a" * 24)
    assert not TOKENS[1].search("task" + "-" + "a" * 24)
    assert SESSION_IDS[0].search("session" + "_0123abcd-ef01-")
    assert SESSION_IDS[1].search("ses" + "_" + "A1" * 12)


def test_license_present():
    assert (ROOT / "LICENSE").is_file()
    assert 'license = "MIT"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_dependencies_have_lower_bounds():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert [dep for dep in project["dependencies"] if ">=" not in dep] == []


HISTORY_REFS = re.compile(
    r"\bT\d+\.\d+\b|\bstage\s*\d|SPEC_|PLAN_|(?i:sp" + "ike)|сп" + "айк|" + "эт" + "ап|сп" + "ек"
)
CYRILLIC = re.compile("[Ѐ-ӿ]")
NON_ASCII_MARK = "non-ascii on purpose"


def test_no_history_refs(texts):
    """Comments and docs describe the code, not the history of its changes."""
    hits = [
        f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
        for name, text in texts
        if name != "tests/test_hygiene.py"
        for match in HISTORY_REFS.finditer(text)
    ]
    assert hits == []


def test_no_cyrillic_in_code(texts):
    """Code is in English; non-ASCII test data is marked explicitly."""
    hits = []
    for name, text in texts:
        if not name.startswith(("src/", "tests/", "integrations/")):
            continue
        if name.startswith("tests/fixtures/") or name == "tests/test_hygiene.py":
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if CYRILLIC.search(line) and NON_ASCII_MARK not in line:
                hits.append(f"{name}:{number}")
    assert hits == []
