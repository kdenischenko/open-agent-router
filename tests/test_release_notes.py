"""Release notes are taken from CHANGELOG.md, and a release fails without them."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"
REPO = "https://github.com/kdenischenko/open-agent-router"


def load_script():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = load_script()

CHANGELOG = """# Changelog

Intro text.

## [Unreleased]

## [1.1.0] - 2026-01-02

### Added

- A long entry that is
  wrapped onto a second line.
- Short entry.

A paragraph that is
wrapped too.

```toml
[tasks]
default_timeout_s = 60
```

## [1.0.0] - 2026-01-01

First release.

[Unreleased]: https://example.com/compare/v1.1.0...HEAD
[1.1.0]: https://example.com/compare/v1.0.0...v1.1.0
[1.0.0]: https://example.com/releases/tag/v1.0.0
"""


def test_notes_take_only_the_section_of_the_version():
    notes = release_notes.notes(CHANGELOG, "1.1.0")

    assert notes.startswith("### Added\n\n")
    assert "Intro text" not in notes
    assert "First release" not in notes


def test_notes_join_wrapped_lines_and_keep_code_blocks():
    notes = release_notes.notes(CHANGELOG, "1.1.0")

    assert "- A long entry that is wrapped onto a second line.\n- Short entry.\n" in notes
    assert "A paragraph that is wrapped too.\n" in notes
    assert "```toml\n[tasks]\ndefault_timeout_s = 60\n```" in notes


def test_notes_of_the_last_section_leave_out_link_definitions():
    notes = release_notes.notes(CHANGELOG, "1.0.0")

    assert notes.startswith("First release.\n")
    assert "example.com/compare" not in notes


def test_notes_end_with_the_install_command():
    notes = release_notes.notes(CHANGELOG, "1.0.0")

    assert notes.endswith("`uv tool install open-agent-router==1.0.0`\n")


@pytest.mark.parametrize("version", ["2.0.0", "Unreleased", "1.1"])
def test_notes_reject_a_missing_or_empty_section(version):
    with pytest.raises(release_notes.NotesError, match="changelog"):
        release_notes.notes(CHANGELOG, version)


def test_main_writes_the_notes(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    output = tmp_path / "notes.md"

    code = release_notes.main(["1.0.0", "--changelog", str(changelog), "--output", str(output)])

    assert code == 0
    assert output.read_text(encoding="utf-8").startswith("First release.\n")


def test_main_reports_a_missing_section(tmp_path: Path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    output = tmp_path / "notes.md"

    code = release_notes.main(["3.0.0", "--changelog", str(changelog), "--output", str(output)])

    assert code == 1
    assert "3.0.0" in capsys.readouterr().err
    assert not output.exists()


def test_changelog_has_notes_for_the_project_version():
    """Bumping the version without a changelog section fails here, before a tag is pushed."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert release_notes.notes(text, version)


def test_changelog_links_every_version_to_its_changes():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
    links = dict(re.findall(r"^\[([^\]]+)\]: (\S+)$", text, re.MULTILINE))

    assert versions[0] == "Unreleased"
    assert links["Unreleased"] == f"{REPO}/compare/v{versions[1]}...HEAD"
    for newer, older in zip(versions[1:], versions[2:], strict=False):
        assert links[newer] == f"{REPO}/compare/v{older}...v{newer}"
    assert links[versions[-1]] == f"{REPO}/releases/tag/v{versions[-1]}"
