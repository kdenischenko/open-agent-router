"""Lightweight Claude Code status line command."""

from __future__ import annotations

import importlib
import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "claude_statusline.json"
TOKEN = "statusline-test-secret"
NO_DATA = "limits: no data\n"


@pytest.fixture(autouse=True)
def no_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def statusline():
    return importlib.import_module("agent_router.statusline")


def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_bytes())


def set_stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))


def write_token(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "client.token").write_text(f"{TOKEN}\n", encoding="utf-8")


class DummyResponse:
    def close(self) -> None:
        pass


def test_import_does_not_load_heavy_dependencies() -> None:
    code = """
import sys
import agent_router.statusline
forbidden = {"fastapi", "httpx", "typer", "platformdirs", "jinja2"}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("loaded: " + ", ".join(loaded))
"""
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")


def test_render_two_windows(statusline) -> None:
    assert render_text(statusline, fixture_payload()["rate_limits"]) == "5h 42% · 7d 61.5%"


def render_text(module, rate_limits) -> str:
    return module.render(rate_limits)


def test_render_five_hour_only(statusline) -> None:
    rate_limits = {"five_hour": {"used_percentage": 42.0}}
    assert statusline.render(rate_limits) == "5h 42%"


def test_render_seven_day_only(statusline) -> None:
    rate_limits = {"seven_day": {"used_percentage": 61.5}}
    assert statusline.render(rate_limits) == "7d 61.5%"


def test_render_empty(statusline) -> None:
    assert statusline.render({}) == "limits: no data"


@pytest.mark.parametrize(
    "rate_limits",
    [
        None,
        {"five_hour": None},
        {"five_hour": {"used_percentage": "42"}},
        {"five_hour": {"used_percentage": 101}},
        {"five_hour": {"used_percentage": True}},
    ],
)
def test_render_invalid_data_is_fallback(statusline, rate_limits) -> None:
    assert statusline.render(rate_limits) == "limits: no data"


def test_post_signal_sends_exact_request(statusline, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return DummyResponse()

    monkeypatch.setattr(statusline, "_urlopen", fake_urlopen)
    rate_limits = fixture_payload()["rate_limits"]

    statusline.post_signal("http://127.0.0.1:8787/", TOKEN, rate_limits, timeout=0.25)

    assert len(calls) == 1
    request, timeout = calls[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:8787/api/v1/signals/claude-statusline"
    assert request.get_method() == "POST"
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"] == "application/json"
    assert json.loads(request.data) == {"rate_limits": rate_limits}
    assert timeout == 0.25


def test_main_sends_only_rate_limits(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_token(home)
    monkeypatch.delenv("AGENT_ROUTER_HOME", raising=False)
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return DummyResponse()

    monkeypatch.setattr(statusline, "_urlopen", fake_urlopen)

    assert statusline.main(["--home", str(home)]) == 0

    assert json.loads(requests[0].data) == {"rate_limits": fixture_payload()["rate_limits"]}
    assert capsys.readouterr().out == "5h 42% · 7d 61.5%\n"


def test_main_uses_home_environment_and_default_port(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_token(home)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return DummyResponse()

    monkeypatch.setattr(statusline, "_urlopen", fake_urlopen)

    assert statusline.main([]) == 0
    assert requests[0].full_url.startswith("http://127.0.0.1:8787/")
    capsys.readouterr()


def test_main_uses_configured_port(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_token(home)
    (home / "config.toml").write_text("[server]\nport = 8899\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return DummyResponse()

    monkeypatch.setattr(statusline, "_urlopen", fake_urlopen)

    assert statusline.main([]) == 0
    assert requests[0].full_url.startswith("http://127.0.0.1:8899/")
    capsys.readouterr()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_main_uses_gateway_url_from_environment(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys, scheme
) -> None:
    write_token(home)
    (home / "config.toml").write_text("[server]\nport = 8899\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setenv("AGENT_ROUTER_URL", f"{scheme}://127.0.0.1:9911/")
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return DummyResponse()

    monkeypatch.setattr(statusline, "_urlopen", fake_urlopen)

    assert statusline.main([]) == 0
    assert requests[0].full_url == f"{scheme}://127.0.0.1:9911/api/v1/signals/claude-statusline"
    capsys.readouterr()


@pytest.mark.parametrize(
    "url",
    ["file:///gateway", "file://[", "http://[", "ftp://", "data:application/json,{}", "gateway"],
)
def test_main_ignores_unsupported_gateway_url_and_keeps_output(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys, url
) -> None:
    write_token(home)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setenv("AGENT_ROUTER_URL", url)
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    calls = []
    monkeypatch.setattr(
        statusline,
        "_urlopen",
        lambda request, timeout: calls.append(request) or DummyResponse(),
    )

    assert statusline.main([]) == 0
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == "5h 42% · 7d 61.5%\n"
    assert captured.err == ""


@pytest.mark.parametrize("use_option", [False, True], ids=["env", "option"])
def test_statusline_expands_tilde_from_env_and_option(
    statusline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_option: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = tmp_path / "ar"
    write_token(home)
    (home / "config.toml").write_text("[server]\nport = 8899\n", encoding="utf-8")
    if use_option:
        monkeypatch.delenv("AGENT_ROUTER_HOME", raising=False)
        argv = ["--home", "~/ar"]
    else:
        monkeypatch.setenv("AGENT_ROUTER_HOME", "~/ar")
        argv = []
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    requests = []
    monkeypatch.setattr(
        statusline,
        "_urlopen",
        lambda request, timeout: requests.append(request) or DummyResponse(),
    )

    assert statusline.main(argv) == 0

    assert len(requests) == 1
    assert requests[0].full_url == "http://127.0.0.1:8899/api/v1/signals/claude-statusline"
    assert json.loads(requests[0].data) == {"rate_limits": fixture_payload()["rate_limits"]}


def test_main_without_rate_limits_does_not_send_signal(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_token(home)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, b'{"session_id": "not-sent"}')
    calls = []
    monkeypatch.setattr(
        statusline,
        "_urlopen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert statusline.main([]) == 0
    assert calls == []
    assert capsys.readouterr().out == NO_DATA


def test_main_without_home_does_not_send_signal(
    statusline, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("AGENT_ROUTER_HOME", raising=False)
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    calls = []
    monkeypatch.setattr(
        statusline,
        "_urlopen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert statusline.main([]) == 0
    assert calls == []
    assert capsys.readouterr().out == "5h 42% · 7d 61.5%\n"


def test_main_without_token_does_not_send_signal(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    home.mkdir(parents=True)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, FIXTURE.read_bytes())
    calls = []
    monkeypatch.setattr(
        statusline,
        "_urlopen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert statusline.main([]) == 0
    assert calls == []
    assert capsys.readouterr().out == "5h 42% · 7d 61.5%\n"


def test_garbage_stdin_returns_zero_and_fallback(
    statusline, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("AGENT_ROUTER_HOME", raising=False)
    set_stdin(monkeypatch, b"\xff not json")

    assert statusline.main([]) == 0
    assert capsys.readouterr().out == NO_DATA


def test_post_signal_errors_are_silent_and_hide_token(
    statusline, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def fail(*args, **kwargs):
        raise OSError(f"gateway rejected {TOKEN}")

    monkeypatch.setattr(statusline, "_urlopen", fail)

    statusline.post_signal("http://127.0.0.1:8787", TOKEN, {})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert TOKEN not in captured.out + captured.err


def test_main_gateway_error_keeps_output_and_hides_token(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_token(home)
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, FIXTURE.read_bytes())

    def fail(*args, **kwargs):
        raise OSError(f"gateway rejected {TOKEN}")

    monkeypatch.setattr(statusline, "_urlopen", fail)

    assert statusline.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == "5h 42% · 7d 61.5%\n"
    assert captured.err == ""
    assert TOKEN not in captured.out + captured.err


def test_unavailable_gateway_returns_promptly_with_same_output(
    statusline, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    write_token(home)
    (home / "config.toml").write_text(f"[server]\nport = {port}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    set_stdin(monkeypatch, FIXTURE.read_bytes())

    started = time.monotonic()
    assert statusline.main([]) == 0
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert capsys.readouterr().out == "5h 42% · 7d 61.5%\n"


def test_main_writes_utf8_and_returns_zero_in_real_process() -> None:
    env = os.environ.copy()
    env.pop("AGENT_ROUTER_HOME", None)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_router.statusline import main; raise SystemExit(main())",
        ],
        input=FIXTURE.read_bytes(),
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert done.returncode == 0
    assert done.stdout.decode("utf-8") == "5h 42% · 7d 61.5%\n"
    assert done.stderr == b""


def test_post_signal_does_not_follow_redirect_with_token(statusline) -> None:
    """A foreign process on the gateway port must not redirect the token to another host."""
    import http.server
    import threading

    seen: list[str | None] = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args) -> None:
            pass

    sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    target = f"http://127.0.0.1:{sink.server_address[1]}/stolen"

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    trap = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (sink, trap)]
    for thread in threads:
        thread.start()
    try:
        url = f"http://127.0.0.1:{trap.server_address[1]}"
        statusline.post_signal(url, TOKEN, fixture_payload()["rate_limits"], timeout=2)
    finally:
        for server in (sink, trap):
            server.shutdown()
            server.server_close()
    assert seen == []


def test_statusline_ignores_proxy_env(statusline, monkeypatch: pytest.MonkeyPatch) -> None:
    """The system proxy must not receive the status line token.

    urllib handlers read proxy settings when built, i.e. at import time: the module is
    reloaded with proxy variables in the environment, as in the status line process.
    """
    from helpers import http_server, set_proxy_env

    try:
        with monkeypatch.context() as patched:
            set_proxy_env(patched)
            importlib.reload(statusline)
            with http_server() as (url, seen):
                statusline.post_signal(url, TOKEN, fixture_payload()["rate_limits"], timeout=2)
    finally:
        importlib.reload(statusline)
    assert [request.path for request in seen] == ["/api/v1/signals/claude-statusline"]
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
