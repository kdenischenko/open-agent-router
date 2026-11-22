"""CLI commands report a broken gateway home without a traceback."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from agent_router import cli, cli_common


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(True))
    return calls


def _clean_failure(result) -> None:
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception


@pytest.mark.parametrize("name", ["admin.token", "client.token"])
def test_init_reports_empty_token_file(home: Path, name: str) -> None:
    home.mkdir(parents=True)
    (home / name).write_text("\n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["init"])

    _clean_failure(result)
    assert f"empty token file: {home / name}" in result.output
    assert "delete it and run agent-router init" in result.output


def test_serve_reports_empty_token_file(home: Path, config, started: list[bool]) -> None:
    (home / "admin.token").write_text("", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["serve"])

    _clean_failure(result)
    assert f"empty token file: {home / 'admin.token'}" in result.output
    assert started == []


def test_client_command_reports_empty_token_file(home: Path, config, tokens) -> None:
    (home / "client.token").write_text("  \n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["status"])

    _clean_failure(result)
    assert f"empty token file: {home / 'client.token'}" in result.output
    assert "delete it and run agent-router init" in result.output


def test_client_command_without_tokens_points_to_init(home: Path, config) -> None:
    result = CliRunner().invoke(cli.app, ["status"])

    _clean_failure(result)
    assert "failed to read tokens; run agent-router init" in result.output


@pytest.mark.parametrize(
    "url",
    ["file:///gateway", "file://[", "http://[", "ftp://", "data:application/json,{}", "gateway"],
)
def test_client_command_reports_unsupported_url_scheme(config, tokens, monkeypatch, url) -> None:
    monkeypatch.setenv("AGENT_ROUTER_URL", url)

    result = CliRunner().invoke(cli.app, ["status"])

    _clean_failure(result)
    assert "config error: AGENT_ROUTER_URL must use http or https" in result.output


@pytest.mark.parametrize("url", ["http://127.0.0.1:8787", "https://gateway.example"])
def test_gateway_accepts_http_and_https(config, tokens, monkeypatch, url) -> None:
    monkeypatch.setenv("AGENT_ROUTER_URL", url)

    assert cli_common._gateway().base_url == url


def test_serve_reports_damaged_store(home: Path, config, tokens, started: list[bool]) -> None:
    (home / "agent-router.sqlite").write_bytes(b"not a database" * 100)

    result = CliRunner().invoke(cli.app, ["serve"])

    _clean_failure(result)
    assert "failed to open the store" in result.output
    assert started == []


def test_doctor_reports_damaged_store(home: Path, config, tokens) -> None:
    (home / "agent-router.sqlite").write_bytes(b"not a database" * 100)

    result = CliRunner().invoke(cli.app, ["doctor"])

    _clean_failure(result)
    assert "Store: error:" in result.output


def _fail_to_start(*args, **kwargs) -> None:
    sys.exit(3)  # what uvicorn does when it cannot bind or start


def test_serve_names_a_busy_port(home: Path, config, tokens, monkeypatch) -> None:
    monkeypatch.setattr(uvicorn, "run", _fail_to_start)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])

    _clean_failure(result)
    assert f"port {port} on 127.0.0.1 is in use" in result.output
    assert "agent-router serve --port" in result.output


def test_serve_reports_other_startup_failures(home: Path, config, tokens, monkeypatch) -> None:
    monkeypatch.setattr(uvicorn, "run", _fail_to_start)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]

    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])

    _clean_failure(result)
    assert "gateway failed to start: see the error above" in result.output
