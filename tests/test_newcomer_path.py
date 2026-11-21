"""The first run a new user follows, end to end: the steps `init` prints, run as printed.

The gateway runs as its own `agent-router serve` process; a stand-in for the Kimi Code CLI is the
only harness on PATH, so the real config template, adapter, runner and dashboard take part.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from agent_router.auth import ensure_tokens
from agent_router.cli import app

STUB = Path(__file__).parent / "stubs" / "fake_kimi.py"
CLI = "from agent_router.cli import main; main()"
STEP = re.compile(r"^  (?P<name>\w+): agent-router (?P<command>.+)$")


def install_fake_kimi(directory: Path) -> None:
    """A `kimi` on PATH: a .cmd shim on Windows, as npm and uv tools install it there."""
    directory.mkdir()
    if sys.platform == "win32":
        shim = f'@"{sys.executable}" "{STUB}" %*\r\n'
        (directory / "kimi.cmd").write_text(shim, encoding="ascii")
        return
    script = directory / "kimi"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)


def search_path(bin_dir: Path) -> str:
    """PATH with the stand-in, git and system tools only, so no installed harness is found."""
    git = shutil.which("git")
    assert git is not None, "the gateway needs git on PATH"
    parts = [str(bin_dir)]
    if sys.platform == "win32":
        # Git for Windows finds its runtime next to git.exe, so its directory goes on PATH
        parts.append(str(Path(git).parent))
        parts.append(str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"))
    else:
        (bin_dir / "git").symlink_to(git)
    return os.pathsep.join(parts)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServeProcess:
    """`agent-router serve --port <port>` in a child process with its output collected."""

    def __init__(self, env: dict[str, str], port: int) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", CLI, "serve", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.lines: list[str] = []
        self.reader = threading.Thread(target=self._read, name="serve-output", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            self.lines.append(raw.decode("utf-8", "replace").rstrip())

    def output(self) -> str:
        return "\n".join(self.lines)

    def wait_ready(self, port: int, timeout_s: float = 60) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            assert self.process.poll() is None, f"serve exited:\n{self.output()}"
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, f"serve did not start:\n{self.output()}"
            time.sleep(0.1)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(30)
        self.reader.join(10)


@pytest.fixture
def newcomer(tmp_path, monkeypatch) -> Path:
    """Gateway home and PATH of a user whose only harness is Kimi Code; returns the project."""
    home = tmp_path / "дом шлюза"  # non-ascii on purpose
    project = tmp_path / "мой проект"  # a space and non-ascii on purpose
    project.mkdir()
    install_fake_kimi(tmp_path / "bin")
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setenv("PATH", search_path(tmp_path / "bin"))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def start_serve(newcomer) -> Iterator[Callable[[int], ServeProcess]]:
    started: list[ServeProcess] = []

    def start(port: int) -> ServeProcess:
        started.append(ServeProcess(dict(os.environ), port))
        return started[-1]

    yield start
    for process in started:
        process.stop()


def steps_from(output: str) -> dict[str, list[str]]:
    """The `name: agent-router ...` lines of init as argument lists, as a shell would split them."""
    steps = {}
    for line in output.splitlines():
        found = STEP.match(line)
        if found:
            steps[found["name"]] = shlex.split(found["command"])
    return steps


def test_the_steps_init_prints_run_a_first_job(newcomer, start_serve, monkeypatch):
    runner = CliRunner()
    init = runner.invoke(app, ["init", "--allowed-root", str(newcomer)])
    assert init.exit_code == 0, init.output
    assert "Harnesses found: kimi" in init.output
    steps = steps_from(init.output)
    assert list(steps) == ["serve", "check", "run", "approve", "wait"]
    assert steps["serve"][0] == "serve"

    port = free_port()
    process = start_serve(port)
    process.wait_ready(port)
    hint = re.search(r"set AGENT_ROUTER_URL=(\S+) for client commands", process.output())
    assert hint is not None, process.output()
    assert hint[1] == f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AGENT_ROUTER_URL", hint[1])

    check = runner.invoke(app, steps["check"])
    assert check.exit_code == 0, check.output
    assert "kimi-cli/" in check.output

    run = runner.invoke(app, steps["run"])
    assert run.exit_code == 0, run.output
    job_id = run.stdout.strip()

    def with_id(args: list[str]) -> list[str]:
        return [job_id if arg == "<job-id>" else arg for arg in args]

    approve = runner.invoke(app, with_id(steps["approve"]))
    assert approve.exit_code == 0, approve.output
    assert f"approved: follow the job with agent-router wait {job_id}" in approve.stderr

    wait = runner.invoke(app, with_id(steps["wait"]))
    assert wait.exit_code == 0, wait.output
    assert "read: Describe this repository" in wait.stdout

    admin_token = ensure_tokens(Path(os.environ["AGENT_ROUTER_HOME"])).admin
    with httpx.Client(base_url=hint[1], timeout=10) as browser:
        login = browser.post("/login", data={"token": admin_token}, follow_redirects=False)
        assert login.status_code == 303
        page = browser.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Describe this repository" in page.text
        assert "completed" in page.text
