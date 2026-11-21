"""Test helpers: process checks, temporary git repositories, local HTTP server."""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Whether the process is alive. On Windows os.kill would kill it, so tasklist is used."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        ).stdout.decode("utf-8", "replace")
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=cwd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )
    return done.stdout.decode("utf-8", "replace")


def make_empty_git_repo(path: Path) -> Path:
    """Creates a repository with no commits and a local author identity."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")
    return path


def make_git_repo(path: Path) -> Path:
    """Creates a repository with one commit and a local author identity."""
    make_empty_git_repo(path)
    (path / "README.md").write_text("test repo\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def break_index(repo: Path) -> None:
    """Breaks the repository index: `git rev-parse` works, `git status` fails."""
    (repo / ".git" / "index").write_bytes(b"not an index")


def _sqlite_store(tmp_path: Path):
    from agent_router.store import SqliteStore

    return SqliteStore(tmp_path / "agent-router.sqlite")


# backend name → store factory; Store protocol tests iterate over all keys
STORE_FACTORIES = {"sqlite": _sqlite_store}

BASE_URL = "http://127.0.0.1:8787"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ALL_HARNESSES = ("claude", "cline", "kimi", "opencode")


# test limits of the usd pool windows (5h, week, month) in place of the template placeholders
TEST_USD_LIMITS = (12, 30, 60)


def full_config_text(*, confirm: bool = True) -> str:
    """The init template with every harness found: all routes active, fixed usd limits.

    ``confirm=False`` turns off the confirmation of every route.
    """
    from agent_router.config import default_config_text

    text = default_config_text(ALL_HARNESSES)
    for limit in TEST_USD_LIMITS:
        text = text.replace("limit_usd = 10  # set your plan limit", f"limit_usd = {limit}", 1)
    assert "# set your plan limit" not in text
    if not confirm:
        text = text.replace("confirm = true", "confirm = false")
    return text


def read_only_route(text: str, route_id: str) -> str:
    """Config text in which the given route serves read jobs only."""
    both = 'modes = ["read", "write"]'
    at = text.index(both, text.index(f'id = "{route_id}"'))
    return text[:at] + 'modes = ["read"]' + text[at + len(both) :]


def write_config(home: Path, text: str):
    """Writes config.toml into the gateway directory and returns the parsed config."""
    from agent_router.config import load_config

    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _sqlite_jobstore(store):
    from agent_router.jobstore import SqliteJobStore

    return SqliteJobStore(store)


# backend name → job-store factory over a ready Store
JOBSTORE_FACTORIES = {"sqlite": _sqlite_jobstore}


@dataclass
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@contextmanager
def http_server(
    status: int = 200, body: bytes = b"{}", headers: dict[str, str] | None = None
) -> Iterator[tuple[str, list[SeenRequest]]]:
    """Local HTTP server in a thread: answers every request the same way and records requests."""
    seen: list[SeenRequest] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            seen.append(
                SeenRequest(
                    self.command, self.path, dict(self.headers.items()), self.rfile.read(length)
                )
            )
            self.send_response(status)
            for name, value in (headers or {"Content-Type": "application/json"}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def closed_port_url() -> str:
    """An address on loopback where nothing is listening."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def set_proxy_env(monkeypatch) -> None:
    """Point proxy environment variables at a closed port, with no loopback exceptions."""
    proxy = closed_port_url()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
