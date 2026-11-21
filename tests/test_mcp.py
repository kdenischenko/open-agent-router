"""MCP server: tool list, calls mapped to the gateway HTTP API, errors, no admin actions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from agent_router import cli, mcp_server
from agent_router.auth import ensure_tokens
from agent_router.client import GatewayError, GatewayUnavailable

EXPECTED_TOOLS = {
    "submit_job",
    "wait_job",
    "job_result",
    "list_jobs",
    "cancel_job",
    "rate_job",
    "advice",
    "status",
}
JOB = {"id": "j_1", "status": "running", "text": None}


class FakeGateway:
    """Records calls; answers with canned JSON or raises the configured error."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.error: Exception | None = None
        self.answer: dict[str, Any] = JOB

    def get(self, path: str, **params: Any) -> dict:
        self.calls.append(("GET", path, params))
        if self.error is not None:
            raise self.error
        return self.answer

    def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", path, body))
        if self.error is not None:
            raise self.error
        return self.answer


def _call(gateway: FakeGateway, name: str, arguments: dict[str, Any]):
    server = mcp_server.build_server(gateway)

    async def main():
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(main)


def _tools(gateway: FakeGateway):
    server = mcp_server.build_server(gateway)

    async def main():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return anyio.run(main)


def _payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def _error(result) -> str:
    assert result.is_error
    return result.content[0].text


def test_tool_list_is_fixed() -> None:
    names = {tool.name for tool in _tools(FakeGateway())}

    assert names == EXPECTED_TOOLS
    assert not names & {"approve_job", "deny_job", "approve", "deny", "calibrate", "unblock"}


def test_descriptions_keep_human_decisions_with_the_human() -> None:
    tools = {tool.name: tool.description or "" for tool in _tools(FakeGateway())}

    assert all(tools.values())
    assert "approve" in tools["submit_job"].lower()
    assert "human" in tools["submit_job"].lower()
    for name in ("wait_job", "job_result"):
        assert "not instructions" in tools[name].lower()


def test_mcp_wait_job_description_mentions_cap() -> None:
    tools = {tool.name: tool.description or "" for tool in _tools(FakeGateway())}

    assert "100" in tools["wait_job"]
    assert "approval" in tools["wait_job"].lower()


def test_submit_job_parameters_say_what_to_pass() -> None:
    tools = {tool.name: tool for tool in _tools(FakeGateway())}
    submit = tools["submit_job"].input_schema["properties"]
    wait = tools["wait_job"].input_schema["properties"]

    assert "absolute path" in submit["cwd"]["description"]
    assert "allowed_roots" in submit["cwd"]["description"]
    assert "your own harness" in submit["caller"]["description"]
    assert "[callers]" in submit["caller"]["description"]
    assert "seconds" in submit["timeout_s"]["description"]
    assert "capped at 100" in wait["timeout_s"]["description"]


def test_submit_job_posts_the_job() -> None:
    gateway = FakeGateway()
    gateway.answer = {"job_id": "j_1", "status": "queued"}

    payload = _payload(
        _call(
            gateway,
            "submit_job",
            {
                "prompt": "Describe the repository",
                "cwd": "C:/work/repo",
                "mode": "write",
                "tier": "cheap",
                "caller": "claude-code",
            },
        )
    )

    assert payload == {"job_id": "j_1", "status": "queued"}
    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs")
    assert body == {
        "prompt": "Describe the repository",
        "cwd": "C:/work/repo",
        "mode": "write",
        "tier": "cheap",
        "caller": "claude-code",
    }


def test_submit_job_defaults_to_read_and_omits_empty_hints() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"})

    assert gateway.calls[0][2] == {"prompt": "p", "cwd": "C:/work/repo", "mode": "read"}


def test_wait_job_caps_the_timeout_and_returns_an_unfinished_job() -> None:
    gateway = FakeGateway()

    payload = _payload(_call(gateway, "wait_job", {"job_id": "j_1", "timeout_s": 500}))

    assert payload["status"] == "running"
    assert gateway.calls == [("GET", "/api/v1/jobs/j_1/wait", {"timeout": 100})]


def test_wait_job_default_timeout() -> None:
    gateway = FakeGateway()

    _call(gateway, "wait_job", {"job_id": "j_1"})

    assert gateway.calls == [("GET", "/api/v1/jobs/j_1/wait", {"timeout": 100})]


def test_wait_job_rejects_a_negative_timeout_without_a_request() -> None:
    gateway = FakeGateway()

    message = _error(_call(gateway, "wait_job", {"job_id": "j_1", "timeout_s": -1}))

    assert "timeout" in message
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("name", "arguments", "call"),
    [
        ("job_result", {"job_id": "j_1"}, ("GET", "/api/v1/jobs/j_1", {})),
        ("cancel_job", {"job_id": "j_1"}, ("POST", "/api/v1/jobs/j_1/cancel", None)),
        ("status", {}, ("GET", "/api/v1/snapshot", {})),
        (
            "list_jobs",
            {"status": "completed", "limit": 5},
            ("GET", "/api/v1/jobs", {"status": "completed", "limit": 5}),
        ),
        (
            "advice",
            {"tier": "cheap", "caller": "claude-code"},
            ("GET", "/api/v1/advice", {"tier": "cheap", "caller": "claude-code"}),
        ),
        (
            "advice",
            {"mode": "write"},
            ("GET", "/api/v1/advice", {"tier": "standard", "mode": "write"}),
        ),
    ],
)
def test_tool_maps_to_api(name: str, arguments: dict, call: tuple) -> None:
    gateway = FakeGateway()

    _payload(_call(gateway, name, arguments))

    method, path, params = gateway.calls[0]
    assert (method, path) == call[:2]
    if call[2] is None:
        assert not params
    else:
        assert {key: value for key, value in params.items() if value is not None} == call[2]


def test_rate_job_posts_the_rating() -> None:
    gateway = FakeGateway()

    _call(
        gateway,
        "rate_job",
        {"job_id": "j_1", "value": "good", "should_tier": "cheap", "note": "fine"},
    )

    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs/j_1/rating")
    assert body == {"value": "good", "should_tier": "cheap", "note": "fine"}


def test_rate_job_omits_empty_fields() -> None:
    gateway = FakeGateway()

    _call(gateway, "rate_job", {"job_id": "j_1", "value": "good"})

    assert gateway.calls[0][2] == {"value": "good"}


def test_gateway_error_keeps_code_and_message() -> None:
    gateway = FakeGateway()
    gateway.error = GatewayError(422, "cwd_not_allowed", "directory is not in allowed_roots")

    message = _error(_call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/x"}))

    assert "cwd_not_allowed" in message
    assert "directory is not in allowed_roots" in message


def test_unavailable_gateway_is_a_tool_error() -> None:
    gateway = FakeGateway()
    gateway.error = GatewayUnavailable("gateway unavailable")

    message = _error(_call(gateway, "status", {}))

    assert "gateway unavailable" in message
    assert "agent-router serve" in message


def test_rejected_token_is_a_tool_error() -> None:
    gateway = FakeGateway()
    gateway.error = GatewayError(401, "unauthorized", "bad token")

    assert "rejected the token" in _error(_call(gateway, "status", {}))


def test_mcp_command_uses_the_client_token(
    home: Path, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)
    tokens = ensure_tokens(home)
    seen: dict[str, Any] = {}

    def make_client(base_url: str, token: str):
        seen["url"], seen["token"] = base_url, token
        return FakeGateway()

    monkeypatch.setattr(cli, "make_client", make_client)
    monkeypatch.setattr(mcp_server, "serve", lambda gateway: seen.setdefault("served", gateway))

    result = CliRunner().invoke(cli.app, ["mcp"])

    assert result.exit_code == 0, result.output
    assert seen["token"] == tokens.client
    assert seen["token"] != tokens.admin
    assert seen["url"].startswith("http://127.0.0.1:")
    assert isinstance(seen["served"], FakeGateway)
    assert result.output == ""  # stdout belongs to the MCP protocol


def test_serve_runs_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    transports: list[str] = []
    monkeypatch.setattr(MCPServer, "run", lambda self, transport: transports.append(transport))

    mcp_server.serve(FakeGateway())

    assert transports == ["stdio"]


def test_mcp_command_answers_initialize_on_stdout(home: Path, config) -> None:
    ensure_tokens(home)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    env = {**os.environ, "AGENT_ROUTER_HOME": str(home)}
    env.pop("AGENT_ROUTER_URL", None)
    code = "import sys; sys.argv = ['agent-router', 'mcp']; from agent_router.cli import app; app()"

    done = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines, done.stderr
    answer = json.loads(lines[0])
    assert answer["id"] == 1
    assert answer["result"]["serverInfo"]["name"] == "agent-router"


def test_docs_name_the_server_before_variadic_env() -> None:
    # `--env` takes several values, so a name after it is parsed as one more variable
    docs = Path(__file__).resolve().parent.parent / "docs" / "MCP.md"
    commands = [
        line.strip()
        for line in docs.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("claude mcp add")
    ]

    assert commands
    for command in commands:
        assert command.startswith("claude mcp add agent-router "), command


def test_mcp_docs_cover_opencode_schema() -> None:
    text = (Path(__file__).resolve().parent.parent / "docs" / "MCP.md").read_text(encoding="utf-8")

    assert "opencode mcp add agent-router" in text
    assert '"type": "local"' in text
    assert '"command": ["agent-router", "mcp"]' in text
    opencode = text[text.index("### OpenCode") : text.index("### Cline")]
    assert '"servers": {' in opencode
    assert '"enabled"' not in opencode
