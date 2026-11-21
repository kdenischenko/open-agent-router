"""GatewayClient: read timeout for long job waits; client without httpx."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

import httpx
import pytest

from agent_router.client import GatewayClient, GatewayError, GatewayUnavailable
from helpers import closed_port_url, http_server, set_proxy_env

TOKEN = "client-test-secret"


def capture() -> tuple[GatewayClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "j-1", "status": "running"})

    client = GatewayClient(
        "http://127.0.0.1:8788", "t", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return client, seen


def test_wait_extends_read_timeout_beyond_wait() -> None:
    client, seen = capture()
    client.get("/api/v1/jobs/j-1/wait", timeout=100)
    assert seen[0].url.params["timeout"] == "100"
    read = seen[0].extensions["timeout"]["read"]
    assert read > 100
    assert seen[0].extensions["timeout"]["connect"] == 5


def test_wait_read_timeout_is_capped() -> None:
    client, seen = capture()
    client.get("/api/v1/jobs/j-1/wait", timeout=10_000)
    assert seen[0].extensions["timeout"]["read"] <= 130


def test_plain_get_keeps_short_timeout_and_drops_none() -> None:
    client, seen = capture()
    client.get("/api/v1/jobs", status=None)
    assert "status" not in seen[0].url.params
    assert seen[0].extensions["timeout"]["read"] == 5


# --- default client: urllib, no proxy, no redirects ------------------------------------------


def test_cli_import_does_not_load_httpx() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agent_router.cli, sys; print('httpx' in sys.modules, 'rich' in sys.modules)",
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout.decode().split() == ["False", "False"]


def test_urllib_client_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    set_proxy_env(monkeypatch)
    with http_server(body=b'{"ok": true}') as (url, seen):
        assert GatewayClient(url, TOKEN).get("/api/v1/snapshot") == {"ok": True}
    assert [request.path for request in seen] == ["/api/v1/snapshot"]
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_redirect_is_gateway_error_with_status() -> None:
    with http_server() as (target, stolen):
        redirect = {"Location": target + "/x"}
        with (
            http_server(status=302, body=b"", headers=redirect) as (url, _),
            pytest.raises(GatewayError) as caught,
        ):
            GatewayClient(url, TOKEN).get("/api/v1/snapshot")
    assert stolen == []
    assert (caught.value.status, caught.value.code, caught.value.message) == (
        302,
        None,
        "gateway rejected the request: HTTP 302",
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, b'{"error": {"code": "x", "message": "m"}}', (404, "x", "m")),
        (500, b"<html>", (500, None, "gateway rejected the request: HTTP 500")),
        (200, b"<html>", (200, None, "gateway returned non-JSON")),
        (200, b"[]", (200, None, "gateway returned non-object JSON")),
    ],
)
def test_client_maps_errors(status: int, body: bytes, expected: tuple) -> None:
    """Parity check: gateway response parsing matches the httpx-based client."""
    with http_server(status=status, body=body) as (url, _), pytest.raises(GatewayError) as caught:
        GatewayClient(url, TOKEN).get("/api/v1/jobs/j-1")
    assert (caught.value.status, caught.value.code, caught.value.message) == expected


def test_client_closed_port_is_unavailable() -> None:
    """Parity check."""
    with pytest.raises(GatewayUnavailable):
        GatewayClient(closed_port_url(), TOKEN).get("/api/v1/snapshot")


def test_urllib_post_sends_json_and_drops_none_params() -> None:
    """Parity check: request body and params match the httpx-based client."""
    with http_server(body=b'{"job_id": "j-1"}') as (url, seen):
        client = GatewayClient(url, TOKEN)
        assert client.post("/api/v1/jobs", {"prompt": "hello"}) == {"job_id": "j-1"}
        client.get("/api/v1/jobs", status=None, limit=5)
    assert seen[0].method == "POST"
    assert seen[0].headers["Content-Type"] == "application/json"
    assert json.loads(seen[0].body.decode("utf-8")) == {"prompt": "hello"}
    assert seen[1].path == "/api/v1/jobs?limit=5"


def test_urllib_wait_uses_extended_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []
    original = urllib.request.OpenerDirector.open

    def spy(self, fullurl, data=None, timeout=None):
        timeouts.append(timeout)
        return original(self, fullurl, data, timeout)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    with http_server(body=b'{"status": "running"}') as (url, _):
        client = GatewayClient(url, TOKEN)
        client.get("/api/v1/jobs/j-1/wait", timeout=100)
        client.get("/api/v1/jobs")
    assert len(timeouts) == 2
    assert timeouts[0] > 100
    assert timeouts[1] == 5
