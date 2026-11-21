"""Help text and version output for the command-line interface."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from agent_router import __version__, cli

_AUTOMATIC_PARAMETERS = {"help", "install_completion", "show_completion"}


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _walk_commands(command, path: tuple[str, ...] = ()) -> Iterator[tuple[str, object]]:
    command_path = (*path, command.name) if command.name else path
    yield " ".join(command_path) or "agent-router", command
    for child in getattr(command, "commands", {}).values():
        yield from _walk_commands(child, command_path)


def test_every_command_has_help() -> None:
    missing = []
    for path, command in _walk_commands(get_command(cli.app)):
        if not (command.help or command.short_help):
            missing.append(f"{path}: command help")
        for param in command.params:
            if param.name in _AUTOMATIC_PARAMETERS:
                continue
            if not getattr(param, "help", None):
                missing.append(f"{path}: {param.name} parameter help")

    assert not missing, "missing help:\n" + "\n".join(missing)


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"agent-router {__version__}\n"


def test_help_lists_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for command in ("run", "serve", "doctor", "init"):
        assert command in result.output
