"""MCP over Streamable HTTP served by the gateway itself at /mcp."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agent_router import mcp_server
from agent_router.api import create_app
from agent_router.auth import ensure_tokens
from agent_router.client import GatewayClient
from agent_router.events import EventBus
from agent_router.jobstore import open_jobstore
from agent_router.service import build_dispatcher
from helpers import BASE_URL, STORE_FACTORIES, bearer, write_config
from test_api_jobs import config_text, fake_backend  # noqa: F401  (fixture)
from test_mcp import EXPECTED_TOOLS

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class Gateway:
    """A gateway app with a real dispatcher; extra config text is appended to the config."""

    def __init__(self, tmp_path, home, store, clock, tokens, extra: str = "") -> None:
        from datetime import UTC

        self.root = tmp_path / "рабочие каталоги"  # non-ascii on purpose
        self.root.mkdir()
        self.config = write_config(home, config_text(self.root) + extra)
        self.tokens = tokens
        self.bus = EventBus(clock)
        self.jobs = open_jobstore(self.config.storage, home, store)
        self.dispatcher = build_dispatcher(
            self.config, store, self.jobs, self.bus, clock, home, UTC
        )
        self.app = create_app(
            self.config,
            store,
            clock,
            tokens,
            home=home,
            bus=self.bus,
            dispatcher=self.dispatcher,
            background=False,
        )

    def cwd(self) -> str:
        path = self.root / "каталог задачи"  # non-ascii on purpose
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class LiveServer:
    """uvicorn on an ephemeral 127.0.0.1 port in a thread."""

    def __init__(self, app: Any) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, name="live-mcp", daemon=True)

    def __enter__(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(60)


@pytest.fixture
def make_gateway(tmp_path, home, store, clock, tokens, fake_backend):  # noqa: F811
    def build(extra: str = "") -> Gateway:
        return Gateway(tmp_path, home, store, clock, tokens, extra)

    return build


@pytest.fixture
def gateway(make_gateway) -> Gateway:
    return make_gateway()


@pytest.fixture
def api(gateway: Gateway) -> Iterator[TestClient]:
    with TestClient(gateway.app, base_url=BASE_URL) as instance:
        yield instance


@pytest.fixture
def live(gateway: Gateway) -> Iterator[LiveServer]:
    with LiveServer(gateway.app) as server:
        yield server


def http_client(token: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120)


def over_http(live: LiveServer, token: str) -> Client:
    return Client(streamable_http_client(f"{live.url}/mcp", http_client=http_client(token)))


def over_stdio_server(live: LiveServer, token: str) -> Client:
    """The stdio server as `agent-router mcp` builds it, connected in-process."""
    return Client(mcp_server.build_server(GatewayClient(live.url, token)))


def tool_view(tools) -> list[tuple[str, str | None, dict]]:
    return sorted((tool.name, tool.description, tool.input_schema) for tool in tools)


def result_view(result) -> tuple[bool, list[Any]]:
    content = []
    for item in result.content:
        try:
            content.append(json.loads(item.text))
        except ValueError:
            content.append(item.text)
    return result.is_error, content


# --- transport ---


def test_lists_the_same_tools_as_stdio(live, gateway) -> None:
    async def main():
        async with over_http(live, gateway.tokens.client) as client:
            remote = (await client.list_tools()).tools
        async with over_stdio_server(live, gateway.tokens.client) as client:
            local = (await client.list_tools()).tools
        return remote, local

    remote, local = anyio.run(main)
    assert {tool.name for tool in remote} == EXPECTED_TOOLS
    assert tool_view(remote) == tool_view(local)


def test_every_tool_returns_what_stdio_returns(
    live,
    gateway,
    tmp_path_factory,
    request,
    clock,
    fake_backend,  # noqa: F811  (fixture)
) -> None:
    # the sequence mutates state (cancel, rate) and a replayed submit returns the live job,
    # so each transport gets its own gateway; job ids are random per store, so each run's
    # own id is replaced by a placeholder before the byte comparison
    tmp_path = tmp_path_factory.mktemp("stdio")
    home = tmp_path / "дом шлюза"  # non-ascii on purpose
    store = STORE_FACTORIES[request.node.callspec.params["store"]](tmp_path)
    second = Gateway(tmp_path, home, store, clock, ensure_tokens(home))

    async def calls(client: Client, cwd: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        submit = {
            "prompt": "test job\nfake: exit 0\n",
            "cwd": cwd,
            "request_id": "same-request",
        }
        out["submit_job"] = result_view(await client.call_tool("submit_job", submit))
        job_id = out["submit_job"][1][0]["job_id"]
        steps = [
            ("wait_job", {"job_id": job_id, "timeout_s": 0}),
            ("job_result", {"job_id": job_id}),
            ("job_progress", {"job_id": job_id}),
            ("list_jobs", {}),
            ("advice", {"tier": "standard"}),
            ("status", {}),
            ("cancel_job", {"job_id": job_id}),
            ("rate_job", {"job_id": job_id, "value": "good"}),
            ("job_result", {"job_id": "j_missing"}),
        ]
        for index, (name, arguments) in enumerate(steps):
            out[f"{index}:{name}"] = result_view(await client.call_tool(name, arguments))
        return json.loads(json.dumps(out).replace(job_id, "j_job"))

    async def main():
        with LiveServer(second.app) as second_live:
            async with over_http(live, gateway.tokens.client) as client:
                remote = await calls(client, gateway.cwd())
            async with over_stdio_server(second_live, second.tokens.client) as client:
                local = await calls(client, second.cwd())
        return remote, local

    try:
        remote, local = anyio.run(main)
    finally:
        second.jobs.close()
        store.close()
        second.bus.close()
    assert remote.keys() == local.keys()
    for name in remote:
        if name.endswith(":status"):
            # the snapshot carries its own generation time; the shape must match
            assert remote[name][0] == local[name][0]
            assert remote[name][1][0].keys() == local[name][1][0].keys()
            continue
        assert remote[name] == local[name], name


def test_admin_token_is_accepted(live, gateway) -> None:
    async def main():
        async with over_http(live, gateway.tokens.admin) as client:
            tools = (await client.list_tools()).tools
            status = await client.call_tool("status", {})
            return tools, status

    tools, status = anyio.run(main)
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert not status.is_error, status.content


def test_loopback_client_follows_the_request_port() -> None:
    client = mcp_server.LoopbackClient("token")
    reset = mcp_server.request_port.set(4321)
    try:
        assert client.base_url == "http://127.0.0.1:4321"
    finally:
        mcp_server.request_port.reset(reset)


def test_loopback_client_without_request_port_is_unavailable() -> None:
    from agent_router.client import GatewayUnavailable

    with pytest.raises(GatewayUnavailable):
        mcp_server.LoopbackClient("token").get("/api/v1/snapshot")


def test_long_wait_does_not_block_the_api(live, gateway) -> None:
    """A wait_job held open on /mcp leaves GET /api/v1/snapshot answering at once."""
    submitted = httpx.post(
        f"{live.url}/api/v1/jobs",
        json={"prompt": "test job\nfake: exit 0\n", "cwd": gateway.cwd()},
        headers=bearer(gateway.tokens.client),
        timeout=10,
    )
    assert submitted.status_code == 201, submitted.text
    job_id = submitted.json()["job_id"]  # never ticked: stays queued

    async def main() -> float:
        async with (
            over_http(live, gateway.tokens.client) as client,
            anyio.create_task_group() as group,
        ):
            group.start_soon(client.call_tool, "wait_job", {"job_id": job_id})
            await anyio.sleep(0.5)  # the wait is in flight
            async with httpx.AsyncClient(timeout=10) as plain:
                started = time.monotonic()
                response = await plain.get(
                    f"{live.url}/api/v1/snapshot", headers=bearer(gateway.tokens.client)
                )
                elapsed = time.monotonic() - started
            assert response.status_code == 200
            group.cancel_scope.cancel()
        return elapsed

    assert anyio.run(main) < 5


# --- authentication ---


def post_mcp(api: TestClient, headers: dict[str, str] | None = None, **kwargs: Any):
    return api.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **(headers or {})}, **kwargs)


def test_initialize_with_client_token_succeeds(api, gateway) -> None:
    response = post_mcp(api, bearer(gateway.tokens.client))
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "agent-router"


def test_without_token_is_401(api) -> None:
    response = post_mcp(api)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401(api, gateway) -> None:
    response = post_mcp(api, bearer(gateway.tokens.client + "x"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_non_bearer_scheme_is_401(api, gateway) -> None:
    response = post_mcp(api, {"Authorization": f"Basic {gateway.tokens.client}"})
    assert response.status_code == 401


def test_dashboard_cookie_without_bearer_is_401(api, gateway) -> None:
    login = api.post("/login", data={"token": gateway.tokens.admin}, follow_redirects=False)
    assert login.status_code == 303, login.text
    assert "ar_session" in api.cookies
    response = post_mcp(api)
    assert response.status_code == 401


def test_get_without_token_is_401(api) -> None:
    response = api.get("/mcp", headers=MCP_HEADERS)
    assert response.status_code == 401


# --- Host and Origin ---


def test_foreign_origin_is_403(api, gateway) -> None:
    headers = {**bearer(gateway.tokens.client), "Origin": "http://evil.example"}
    response = post_mcp(api, headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:9999", "https://127.0.0.1:8787", "http://localhost.evil:8787"]
)
def test_loopback_origin_on_another_port_or_scheme_is_403(api, gateway, origin) -> None:
    headers = {**bearer(gateway.tokens.client), "Origin": origin}
    assert post_mcp(api, headers).status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8787", "http://localhost:8787"])
def test_own_loopback_origin_is_accepted(api, gateway, origin) -> None:
    headers = {**bearer(gateway.tokens.client), "Origin": origin}
    response = post_mcp(api, headers)
    assert response.status_code == 200, response.text


def test_foreign_origin_is_403_even_without_token(api) -> None:
    response = post_mcp(api, {"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"


def test_origin_check_leaves_other_paths_alone(api, gateway) -> None:
    response = api.get(
        "/api/v1/snapshot",
        headers={**bearer(gateway.tokens.client), "Origin": "http://evil.example"},
    )
    assert response.status_code == 200


def test_foreign_host_is_rejected(api, gateway) -> None:
    headers = {**bearer(gateway.tokens.client), "Host": "evil.example"}
    response = post_mcp(api, headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_host"


# --- body limit and headers ---


def test_responses_carry_security_headers(api, gateway) -> None:
    response = post_mcp(api, bearer(gateway.tokens.client))
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_oversized_body_is_413(api, gateway) -> None:
    limit = gateway.config.tasks.max_prompt_chars * 12 + 64 * 1024
    headers = {**MCP_HEADERS, **bearer(gateway.tokens.client), "Content-Type": "application/json"}
    response = api.post("/mcp", content=b"x" * (limit + 1), headers=headers)
    assert response.status_code == 413


def test_body_over_the_form_limit_is_accepted(api, gateway) -> None:
    """/mcp gets the API body limit, not the 64 KiB dashboard form limit."""
    padding = "x" * (128 * 1024)
    body = {**INITIALIZE, "params": {**INITIALIZE["params"], "padding": padding}}
    headers = {**MCP_HEADERS, **bearer(gateway.tokens.client)}
    response = api.post("/mcp", json=body, headers=headers)
    assert response.status_code != 413
    assert response.status_code == 200, response.text


def test_portless_loopback_host_is_accepted(api, gateway) -> None:
    headers = {**bearer(gateway.tokens.client), "Host": "127.0.0.1"}
    assert post_mcp(api, headers).status_code == 200


# --- config ---


def test_mcp_http_is_on_by_default(gateway) -> None:
    assert gateway.config.mcp_http is True


def test_mcp_http_false_removes_the_route(make_gateway) -> None:
    gateway = make_gateway("\n[server]\nmcp_http = false\n")
    assert gateway.config.mcp_http is False
    with TestClient(gateway.app, base_url=BASE_URL) as api:
        response = post_mcp(api, bearer(gateway.tokens.client))
        assert response.status_code == 404
        assert api.get("/healthz").status_code == 200


def test_mcp_http_must_be_boolean(home, tmp_path) -> None:
    from agent_router.config import ConfigError

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ConfigError, match="server.mcp_http"):
        write_config(home, config_text(root) + '\n[server]\nmcp_http = "yes"\n')


# --- stdio stays ---


def test_stdio_server_is_unchanged(live, gateway) -> None:
    async def main():
        async with over_stdio_server(live, gateway.tokens.client) as client:
            return (await client.list_tools()).tools

    assert {tool.name for tool in anyio.run(main)} == EXPECTED_TOOLS
