"""MCP server: tool list, calls mapped to the gateway HTTP API, errors, no admin actions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from agent_router import cli, cli_common, mcp_server
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
    assert "route id" in submit["route"]["description"].lower()
    assert "advice" in submit["route"]["description"].lower()
    assert "seconds" in submit["timeout_s"]["description"]
    assert "capped at 100" in wait["timeout_s"]["description"]
    assert "pool" in submit["exclude_pools"]["description"]
    advice = tools["advice"].input_schema["properties"]
    advice_pools = advice["exclude_pools"]["description"].lower()
    assert "pool" in advice_pools
    assert "recommendation" in advice_pools
    assert "job" not in advice_pools
    assert "review" in submit["review_of"]["description"]
    assert "review" in tools["advice"].input_schema["properties"]["review_of"]["description"]


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
                "route": "opencode-cli/glm-5.3",
            },
        )
    )

    assert payload == {"job_id": "j_1", "status": "queued"}
    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs")
    request_id = body.pop("request_id")
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert body == {
        "prompt": "Describe the repository",
        "cwd": "C:/work/repo",
        "mode": "write",
        "tier": "cheap",
        "caller": "claude-code",
        "route": "opencode-cli/glm-5.3",
    }


def test_submit_job_defaults_to_read_and_omits_empty_hints() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"})

    body = gateway.calls[0][2]
    request_id = body.pop("request_id")
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert body == {"prompt": "p", "cwd": "C:/work/repo", "mode": "read"}


def test_submit_job_sends_exclude_pools() -> None:
    gateway = FakeGateway()

    _call(
        gateway,
        "submit_job",
        {"prompt": "p", "cwd": "C:/work/repo", "exclude_pools": ["claude-pro", "kimi-code"]},
    )

    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs")
    assert body["exclude_pools"] == ["claude-pro", "kimi-code"]


def test_submit_job_omits_exclude_pools_when_not_given() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"})

    assert "exclude_pools" not in gateway.calls[0][2]


def test_submit_job_sends_review_of_as_a_review() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo", "review_of": "j_1"})

    method, path, body = gateway.calls[0]
    assert (method, path) == ("POST", "/api/v1/jobs")
    assert body["review_of"] == "j_1"
    assert body["kind"] == "review"


def test_submit_job_omits_review_of_when_not_given() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"})

    body = gateway.calls[0][2]
    assert "review_of" not in body
    assert "kind" not in body


def test_advice_sends_review_of() -> None:
    gateway = FakeGateway()

    _call(gateway, "advice", {"review_of": "j_1"})

    method, path, params = gateway.calls[0]
    assert (method, path) == ("GET", "/api/v1/advice")
    assert params["review_of"] == "j_1"


def test_advice_sends_exclude_pools() -> None:
    gateway = FakeGateway()

    _call(gateway, "advice", {"exclude_pools": ["claude-pro"]})

    method, path, params = gateway.calls[0]
    assert (method, path) == ("GET", "/api/v1/advice")
    assert params["exclude_pools"] == ["claude-pro"]


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


def _job_answer() -> dict[str, Any]:
    return {
        "id": "j_1",
        "status": "completed",
        "route": "opencode-cli/glm-5.3",
        "reason": "most capacity",
        "text": "A long result",
        "changed_files": ["src/example.py"],
        "error": None,
        "log_tail": "executor output",
        "attempts": [{"route": "opencode-cli/glm-5.3"}],
        "created_at": "2026-11-25T12:00:00+00:00",
        "finished_at": "2026-11-25T12:01:00+00:00",
    }


@pytest.mark.parametrize("name", ["wait_job", "job_result"])
def test_job_tools_return_compact_results_by_default(name: str) -> None:
    gateway = FakeGateway()
    gateway.answer = _job_answer()

    payload = _payload(_call(gateway, name, {"job_id": "j_1"}))

    assert payload == {
        "id": "j_1",
        "status": "completed",
        "route": "opencode-cli/glm-5.3",
        "text": "A long result",
        "changed_files": ["src/example.py"],
        "error": None,
    }


@pytest.mark.parametrize("name", ["wait_job", "job_result"])
def test_job_tools_return_the_last_20_log_lines_for_failed_jobs(name: str) -> None:
    gateway = FakeGateway()
    gateway.answer = {
        **_job_answer(),
        "status": "failed",
        "text": None,
        "changed_files": None,
        "error": {"code": "executor_failed", "message": "executor exited"},
        "log_tail": "\n".join(f"line {line}" for line in range(30)),
    }

    payload = _payload(_call(gateway, name, {"job_id": "j_1"}))

    assert payload["log_tail"] == "\n".join(f"line {line}" for line in range(10, 30))
    assert payload["error"] == {"code": "executor_failed", "message": "executor exited"}
    assert "attempts" not in payload


@pytest.mark.parametrize("name", ["wait_job", "job_result"])
def test_job_tools_verbose_returns_full_results(name: str) -> None:
    gateway = FakeGateway()
    gateway.answer = _job_answer()
    tools = {tool.name: tool for tool in _tools(gateway)}

    assert "verbose" in tools[name].input_schema["properties"]
    payload = _payload(_call(gateway, name, {"job_id": "j_1", "verbose": True}))

    assert payload == _job_answer()


def _list_answer() -> dict[str, Any]:
    return {
        "jobs": [
            {
                "id": "j_1",
                "status": "failed",
                "route": "opencode-cli/glm-5.3",
                "mode": "write",
                "caller": "claude-code",
                "created_at": "2026-11-25T12:00:00+00:00",
                "finished_at": "2026-11-25T12:01:00+00:00",
                "error": {"code": "timeout", "message": "job timed out"},
                "prompt": "Describe the repository",
                "text": "A long result",
                "log_tail": "executor output",
                "attempts": [{"route": "opencode-cli/glm-5.3"}],
            }
        ],
        "outer": "preserved",  # stands for any outer key
    }


def test_list_jobs_returns_compact_jobs_by_default() -> None:
    gateway = FakeGateway()
    gateway.answer = _list_answer()

    payload = _payload(_call(gateway, "list_jobs", {}))

    assert payload == {
        "jobs": [
            {
                "id": "j_1",
                "status": "failed",
                "route": "opencode-cli/glm-5.3",
                "mode": "write",
                "caller": "claude-code",
                "created_at": "2026-11-25T12:00:00+00:00",
                "finished_at": "2026-11-25T12:01:00+00:00",
                "error_code": "timeout",
            }
        ],
        "outer": "preserved",
    }


def test_list_jobs_verbose_returns_full_jobs() -> None:
    gateway = FakeGateway()
    gateway.answer = _list_answer()
    tools = {tool.name: tool for tool in _tools(gateway)}

    assert "verbose" in tools["list_jobs"].input_schema["properties"]
    payload = _payload(_call(gateway, "list_jobs", {"verbose": True}))

    assert payload == _list_answer()


def test_list_jobs_missing_error_returns_null_error_code() -> None:
    gateway = FakeGateway()
    gateway.answer = _list_answer()
    del gateway.answer["jobs"][0]["error"]

    payload = _payload(_call(gateway, "list_jobs", {}))

    assert payload["jobs"][0]["error_code"] is None


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
    if name == "list_jobs":
        gateway.answer = {"jobs": []}

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


def test_rate_job_returns_compact_response() -> None:
    gateway = FakeGateway()
    rating = {
        "job_id": "j_1",
        "value": "good",
        "should_tier": "cheap",
        "by": None,
        "note_sha256": None,
        "note_chars": None,
        "at": "2026-11-25T12:00:00+00:00",
    }
    gateway.answer = {
        "id": "j_1",
        "status": "completed",
        "rating": rating,
        "prompt": "Describe the repository",
        "text": "A long result",
        "attempts": [{"route": "opencode-cli/glm-5.3"}],
    }

    payload = _payload(_call(gateway, "rate_job", {"job_id": "j_1", "value": "good"}))

    assert payload == {"id": "j_1", "status": "completed", "rating": rating}


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

    monkeypatch.setattr(cli_common, "make_client", make_client)
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


def test_submit_job_generates_a_request_id() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"})

    assert re.fullmatch(r"[0-9a-f]{32}", gateway.calls[0][2]["request_id"])


def test_submit_job_sends_the_given_request_id() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo", "request_id": "my-id"})

    assert gateway.calls[0][2]["request_id"] == "my-id"


def test_submit_job_retries_a_lost_submit_with_the_same_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(mcp_server, "retry_pause", pauses.append)
    gateway = FakeGateway()
    failures = [GatewayUnavailable("gateway unavailable")] * 2
    plain_post = gateway.post

    def flaky_post(path: str, body: dict | None = None) -> dict:
        if failures:
            gateway.calls.append(("POST", path, body))
            raise failures.pop()
        return plain_post(path, body)

    gateway.post = flaky_post  # type: ignore[method-assign]

    _payload(_call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo"}))

    assert len(gateway.calls) == 3
    assert len({body["request_id"] for _, _, body in gateway.calls}) == 1
    assert len(pauses) == 2


def test_submit_job_forwards_an_empty_request_id() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo", "request_id": ""})

    assert gateway.calls[0][2]["request_id"] == ""


def test_submit_job_sends_the_kind() -> None:
    gateway = FakeGateway()

    _call(gateway, "submit_job", {"prompt": "p", "cwd": "C:/work/repo", "kind": "research"})

    assert gateway.calls[0][2]["kind"] == "research"


def test_submit_job_describes_the_kind() -> None:
    tools = {tool.name: tool for tool in _tools(FakeGateway())}
    description = tools["submit_job"].input_schema["properties"]["kind"]["description"]

    for kind in ("code", "review", "research", "other"):
        assert kind in description


def test_submit_job_rejects_another_kind_with_review_of() -> None:
    gateway = FakeGateway()

    message = _error(
        _call(
            gateway,
            "submit_job",
            {"prompt": "p", "cwd": "C:/work/repo", "kind": "code", "review_of": "j_1"},
        )
    )

    assert "review_of" in message

    assert gateway.calls == []
