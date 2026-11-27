"""Progress of a running attempt: tail reading, the CLI `logs` command and the MCP tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from typer.testing import CliRunner

from agent_router import cli, cli_common, mcp_server
from agent_router.progress import read_tail

PROGRESS = {
    "job": "j_1",
    "status": "running",
    "attempt": {
        "n": 1,
        "route": "a/main",
        "elapsed_s": 125,
        "last_signal": "output",
        "last_signal_age_s": 4,
        "idle_left_s": 596,
        "cap_left_s": 14275,
        "tail": "step 1\nstep 2",
    },
}
DONE = {"job": "j_1", "status": "completed", "attempt": None}


# --- tail reading ----------------------------------------------------------------------


def test_read_tail_returns_only_the_end(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    log.write_bytes(b"x" * 1_000_000 + "конец".encode())

    tail = read_tail(log, max_bytes=64)

    assert tail.endswith("конец")
    assert len(tail.encode()) <= 64


def test_read_tail_drops_a_split_character_at_the_start(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    log.write_bytes("ж".encode() * 10)

    assert read_tail(log, max_bytes=5) == "жж"


def test_read_tail_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "missing.log", max_bytes=64) == ""


# --- CLI -------------------------------------------------------------------------------


class SequenceGateway:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch, config, tokens) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


def use(monkeypatch: pytest.MonkeyPatch, gateway: SequenceGateway) -> None:
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)


def test_logs_prints_the_running_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = SequenceGateway(PROGRESS)
    use(monkeypatch, gateway)

    result = CliRunner().invoke(cli.app, ["logs", "j_1"])

    assert result.exit_code == 0, result.output
    assert gateway.calls[0][0] == "/api/v1/jobs/j_1/progress"
    assert "a/main" in result.output
    assert "step 2" in result.output


def test_logs_without_a_running_attempt_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, SequenceGateway(DONE))

    result = CliRunner().invoke(cli.app, ["logs", "j_1"])

    assert result.exit_code == 0, result.output
    assert "completed" in result.output


def test_logs_follow_stops_when_the_job_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    later = {**PROGRESS, "attempt": {**PROGRESS["attempt"], "tail": "step 2\nstep 3"}}
    gateway = SequenceGateway(PROGRESS, later, DONE)
    use(monkeypatch, gateway)
    monkeypatch.setattr("agent_router.cli_jobs.follow_pause", lambda: None)

    result = CliRunner().invoke(cli.app, ["logs", "j_1", "--follow"])

    assert result.exit_code == 0, result.output
    assert len(gateway.calls) == 3
    assert result.output.count("step 2") == 1  # lines already shown are not repeated
    assert "step 3" in result.output
    assert "completed" in result.output


# --- MCP -------------------------------------------------------------------------------


def _call(gateway: SequenceGateway, name: str, arguments: dict[str, Any]):
    server = mcp_server.build_server(gateway)  # type: ignore[arg-type]

    async def main():
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(main)


def test_job_progress_tool_reads_progress() -> None:
    gateway = SequenceGateway(PROGRESS)

    result = _call(gateway, "job_progress", {"job_id": "j_1"})

    assert gateway.calls[0][0] == "/api/v1/jobs/j_1/progress"
    payload = result.structured_content or {}
    assert payload["status"] == "running"
    assert payload["attempt"]["route"] == "a/main"
    assert "step 2" in payload["attempt"]["tail"]
