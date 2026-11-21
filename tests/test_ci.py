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
