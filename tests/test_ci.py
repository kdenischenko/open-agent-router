"""CI workflow: Windows is required, Linux and macOS run as experimental."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_runs_on_three_systems() -> None:
    text = _workflow()
    assert "os: [windows-latest, ubuntu-latest, macos-latest]" in text
    assert "runs-on: ${{ matrix.os }}" in text
    assert "fail-fast: false" in text


def test_ci_only_windows_is_required() -> None:
    assert "continue-on-error: ${{ matrix.os != 'windows-latest' }}" in _workflow()


def test_ci_runs_the_gates() -> None:
    text = _workflow()
    for command in (
        "uv sync --locked",
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run pytest -q",
    ):
        assert f"run: {command}" in text


def test_ci_ubuntu_suite_runs_without_installed_clis() -> None:
    text = _workflow()
    assert "- name: Test without installed CLIs" in text
    assert "if: matrix.os == 'ubuntu-latest'" in text
    assert "for cli in claude kimi opencode codex cline; do" in text
    assert 'export PATH="/usr/bin:/bin"' in text
    assert '"$uv_bin" run pytest -q' in text
