"""CLI commands and HTTP client calls, exercised against the test app."""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

import pytest
from typer.testing import CliRunner

from agent_router import cli, cli_common, cli_gateway
from agent_router.auth import ensure_tokens
from agent_router.client import GatewayClient
from agent_router.clock import format_local
from agent_router.config import load_config
from helpers import BASE_URL


def test_version_matches_pyproject() -> None:
    """The package version lives only in pyproject.toml; __version__ reads installed metadata."""
    import tomllib

    import agent_router

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    assert agent_router.__version__ == version


def test_distribution_name_and_entry_points() -> None:
    """The distribution is open-agent-router; the module, the command and plugin groups keep the
    agent_router / agent-router names."""
    import tomllib
    from importlib import metadata

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert project["name"] == "open-agent-router"

    dist = metadata.distribution("open-agent-router")
    assert dist.version == project["version"]
    scripts = {entry.name for entry in dist.entry_points.select(group="console_scripts")}
    assert {"agent-router", "agent-router-statusline"} <= scripts
    harnesses = {entry.name for entry in dist.entry_points.select(group="agent_router.harnesses")}
    assert harnesses == {"kimi", "opencode", "claude", "cline"}


def test_release_workflow_uses_trusted_publishing() -> None:
    """Releases are built on a v* tag and published to PyPI over OIDC, with no stored token."""
    workflow = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "v*" in text
    assert "id-token: write" in text
    assert "uv build" in text
    assert "secrets." not in text
    assert "password" not in text


def test_release_workflow_smoke_tests_the_wheel_before_publishing() -> None:
    workflow = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
    text = workflow.read_text(encoding="utf-8")
    build = text.index("run: uv build")
    smoke = text.index("name: Smoke test the wheel")
    publish = text.index("uv publish")

    assert build < smoke < publish
    step = text[smoke:publish]
    assert "dist/*.whl" in step
    assert "--no-project" in step
    assert "AGENT_ROUTER_HOME" in step
    assert "agent-router init" in step
    assert "LASTEXITCODE" in step


def test_release_workflow_publishes_from_its_own_job_and_then_creates_the_release() -> None:
    """Only the publish job may publish, and the GitHub release follows a successful upload."""
    workflow = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
    text = workflow.read_text(encoding="utf-8")
    build = text.index("\n  build:\n")
    publish = text.index("\n  publish:\n")
    release = text.index("\n  github-release:\n")
    assert build < publish < release
    build_job, publish_job, release_job = (
        text[build:publish],
        text[publish:release],
        text[release:],
    )

    assert text.count("id-token: write") == 1
    assert "id-token: write" in publish_job
    assert "environment: pypi" in publish_job
    assert "needs: build" in publish_job
    assert "attest-build-provenance" in publish_job
    assert "uv run" not in publish_job
    assert build_job.index("release_notes.py") < build_job.index("run: uv build")
    assert "needs: publish" in release_job
    assert "contents: write" in release_job
    assert "gh release create" in release_job
    assert "--notes-file" in release_job


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def test_gateway(monkeypatch: pytest.MonkeyPatch, client):
    def factory(base_url: str, token: str) -> GatewayClient:
        return GatewayClient(base_url, token, client=client)

    monkeypatch.setattr(cli_common, "make_client", factory)


def test_init_twice_does_not_change_files(home: Path, runner: CliRunner) -> None:
    first = runner.invoke(cli.app, ["init"])
    paths = [home / name for name in ("config.toml", "client.token", "admin.token")]
    before = [path.read_bytes() for path in paths]

    second = runner.invoke(cli.app, ["init"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert [path.read_bytes() for path in paths] == before


def test_init_does_not_print_token_values(home: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["init"])
    tokens = ensure_tokens(home)

    assert result.exit_code == 0
    assert tokens.client not in result.output
    assert tokens.admin not in result.output


def test_init_enables_only_found(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None
    )

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0
    assert "opencode" in result.output
    assert "limits" in result.output
    config = load_config(home / "config.toml")
    assert [route.id for route in config.routes] == [
        "opencode-cli/glm-5.3-flash",
        "opencode-cli/glm-5.3",
    ]
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert text.count("\n[[routes]]") == 2
    assert text.count("# [[routes]]") == 4


def test_init_skips_broken_harness_plugin(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli_gateway.load_harness

    def load(name: str):
        if name == "kimi":
            raise ImportError("broken plugin")
        return real(name)

    monkeypatch.setattr(cli_gateway, "load_harness", load)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0
    assert "harness kimi failed to load: broken plugin" in result.output
    assert "Harnesses found: claude, cline, opencode" in result.output


def test_init_prints_paths(home: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0
    assert str(home / "config.toml") in result.output
    assert str(home / "client.token") in result.output
    assert str(home / "admin.token") in result.output


def test_init_works_in_home_with_spaces_and_cyrillic(home: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["init"])

    assert " " in str(home)
    assert not str(home).isascii()
    assert result.exit_code == 0
    assert (home / "config.toml").is_file()


def test_status_shows_three_pools(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "claude-pro" in result.output
    assert "kimi-code" in result.output
    assert "opencode-go" in result.output


def test_status_prints_local_reset_time(runner: CliRunner, test_gateway: None) -> None:
    calibrated = runner.invoke(
        cli.app,
        [
            "calibrate",
            "claude-pro",
            "week",
            "42",
            "--resets-at",
            "2026-11-15T00:00:00+00:00",
        ],
    )
    assert calibrated.exit_code == 0

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "+00:00" not in result.output
    week = next(line for line in result.output.splitlines() if "week:" in line)
    reset = week.split("reset: ", 1)[1].split(";", 1)[0]
    assert datetime.strptime(reset, "%d.%m %H:%M")


def test_calibrate_changes_snapshot(runner: CliRunner, test_gateway: None, client, tokens) -> None:
    result = runner.invoke(
        cli.app, ["calibrate", "claude-pro", "week", "42", "--resets-in", "3h20m"]
    )

    assert result.exit_code == 0
    snapshot = GatewayClient(BASE_URL, tokens.client, client=client).get("/api/v1/snapshot")
    pool = next(pool for pool in snapshot["pools"] if pool["id"] == "claude-pro")
    window = next(window for window in pool["windows"] if window["id"] == "week")
    assert window["used"] == pytest.approx(0.42)


def test_calibrate_naive_reset_time_is_sent_with_offset(
    runner: CliRunner, test_gateway: None, client, tokens
) -> None:
    result = runner.invoke(
        cli.app,
        ["calibrate", "claude-pro", "week", "42", "--resets-at", "2026-11-15T00:00:00"],
    )

    assert result.exit_code == 0
    snapshot = GatewayClient(BASE_URL, tokens.client, client=client).get("/api/v1/snapshot")
    pool = next(pool for pool in snapshot["pools"] if pool["id"] == "claude-pro")
    window = next(window for window in pool["windows"] if window["id"] == "week")
    assert datetime.fromisoformat(window["resets_at"]).utcoffset() is not None


def test_advice_defaults_to_standard(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(cli.app, ["advice"])

    assert result.exit_code == 0
    assert "standard" in result.output
    assert "Mode:" not in result.output


class AdviceGateway:
    """Answers /advice with one route excluded by mode and keeps the request parameters."""

    def __init__(self) -> None:
        self.params: dict | None = None

    def get(self, path: str, **params) -> dict:
        self.params = params
        excluded = {
            "route": "kimi-cli/k3.1-pro",
            "pool": "kimi-code",
            "reason": "mode write not supported",
        }
        return {
            "tier": params["tier"],
            "mode": params["mode"],
            "chosen": [],
            "excluded": [excluded],
        }


def test_advice_sends_the_job_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice", "--mode", "write"])

    assert result.exit_code == 0
    assert gateway.params == {"tier": "standard", "caller": None, "mode": "write"}
    assert result.output.splitlines() == [
        "Tier: standard",
        "Mode: write",
        "Excluded: kimi-cli/k3.1-pro (kimi-code) — mode write not supported",
    ]


def test_advice_sends_exclude_pools(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(
        cli.app,
        ["advice", "--exclude-pool", "claude-pro", "--exclude-pool", "kimi-code"],
    )

    assert result.exit_code == 0
    assert gateway.params["exclude_pools"] == ["claude-pro", "kimi-code"]


def test_advice_omits_exclude_pools_without_the_option(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice"])

    assert result.exit_code == 0
    assert "exclude_pools" not in gateway.params


def test_advice_sends_review_of(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice", "--review-of", "j_1"])

    assert result.exit_code == 0
    assert gateway.params["review_of"] == "j_1"


def test_advice_omits_review_of_without_the_option(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice"])

    assert result.exit_code == 0
    assert "review_of" not in gateway.params


def test_advice_prints_the_decision_note(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    class NotedGateway(AdviceGateway):
        def get(self, path: str, **params) -> dict:
            answer = super().get(path, **params)
            answer["note"] = "review runs on the author's model: nothing else was eligible"
            return answer

    gateway = NotedGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice"])

    assert result.exit_code == 0
    assert "Note: review runs on the author's model: nothing else was eligible" in result.output


def test_unavailable_service_returns_five_with_clear_message(
    home: Path, config, tokens, runner: CliRunner
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("AGENT_ROUTER_URL", f"http://127.0.0.1:{port}")
    try:
        result = runner.invoke(cli.app, ["status"])
    finally:
        monkeypatch.undo()

    assert result.exit_code == 5
    assert "unavailable" in result.output.lower()
    assert tokens.client not in result.output
    assert tokens.admin not in result.output


def test_rejected_token_returns_five_without_printing_it(
    runner: CliRunner, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_token = "wrong-token-for-cli-test"
    monkeypatch.setattr(
        cli_common,
        "make_client",
        lambda base_url, token: GatewayClient(base_url, wrong_token, client=client),
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 5
    assert wrong_token not in result.output


def test_rejected_request_returns_six(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(cli.app, ["calibrate", "unknown", "week", "42"])

    assert result.exit_code == 6
    assert "unknown pool" in result.output


def test_statusline_command_quotes_posix_path() -> None:
    home = Path("gateway $(x) 'quoted'")

    command = cli.statusline_command(home, platform="linux")

    assert shlex.split(command) == ["agent-router-statusline", "--home", str(home)]


@pytest.mark.parametrize(
    ("home", "expected"),
    [
        (
            PureWindowsPath("C:/gateway dir"),
            'agent-router-statusline --home "C:/gateway dir"',
        ),
        (PureWindowsPath("C:/"), 'agent-router-statusline --home "C:/"'),
        (
            PureWindowsPath("C:/gateway/"),
            'agent-router-statusline --home "C:/gateway"',
        ),
        *[(PureWindowsPath(f"C:/bad{char}home"), None) for char in '"%!^&|<>$`'],
    ],
)
def test_statusline_command_windows(home: PureWindowsPath, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            cli.statusline_command(home, platform="win32")
        return

    command = cli.statusline_command(home, platform="win32")

    assert command == expected
    assert "\\" not in command


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("character", ["\n", "\r"])
def test_statusline_command_rejects_line_breaks(platform: str, character: str) -> None:
    with pytest.raises(ValueError):
        cli.statusline_command(Path(f"bad{character}home"), platform=platform)


def test_statusline_snippet_is_json(home: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["doctor", "--statusline-snippet"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "statusLine": {
            "type": "command",
            "command": cli.statusline_command(home),
        }
    }


def test_doctor_snippet_expands_relative_home(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ROUTER_HOME", "rel/home")

    result = runner.invoke(cli.app, ["doctor", "--statusline-snippet"])

    assert result.exit_code == 0
    command = json.loads(result.stdout)["statusLine"]["command"]
    assert command == cli.statusline_command(tmp_path / "rel" / "home")


def test_doctor_snippet_home_option_cannot_be_mistaken_for_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ROUTER_HOME", "-x")

    result = runner.invoke(cli.app, ["doctor", "--statusline-snippet"])

    assert result.exit_code == 0
    command = json.loads(result.stdout)["statusLine"]["command"]
    home_arg = shlex.split(command, posix=sys.platform != "win32")[-1].strip('"')
    assert not home_arg.startswith("-")


def test_utf8_output_posix_terminal_only_relaxes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    class Terminal(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    stdout = Terminal(io.BytesIO(), encoding="latin-1")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(sys, "platform", "linux")

    cli._utf8_output()

    assert stdout.encoding == "latin-1"
    assert stdout.errors == "replace"


def test_doctor_snippet_refuses_unsafe_home(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", f"{home}\nunsafe")

    result = runner.invoke(cli.app, ["doctor", "--statusline-snippet"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        "gateway home directory path is unsafe for the command line: "
        "set AGENT_ROUTER_HOME to a simple path\n"
    )


def test_doctor_prints_subscriber_counters(runner: CliRunner, test_gateway: None, bus) -> None:
    bus.subscribe("sink:test:0", lambda event: None)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "sink:test:0" in result.output
    assert "delivered=0" in result.output
    assert "dropped=0" in result.output
    assert "errors=0" in result.output


def test_doctor_warns_about_dropped_events(runner: CliRunner, test_gateway: None, bus) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block(event) -> None:
        entered.set()
        release.wait(5)

    bus.subscribe("sink:test:0", block)
    bus.publish("test.started")
    assert entered.wait(2)
    for _ in range(1001):
        bus.publish("test.overflow")
    try:
        result = runner.invoke(cli.app, ["doctor"])
    finally:
        release.set()

    assert result.exit_code == 0
    assert "dropped=" in result.output
    assert "warning" in result.output.lower()


def test_help_survives_cp1252() -> None:
    """click renders --help while parsing, before any command runs; the UTF-8 stream setup in
    main() must therefore happen before the app is called."""
    import os
    import subprocess

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, "-c", "from agent_router.cli import main; main()", "doctor", "--help"],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    # the doctor help text contains an em dash, which cp1252 cannot encode
    assert "—" in result.stdout.decode("utf-8")


def test_cli_output_survives_non_utf8_pipe(tmp_path) -> None:
    """Piped output on Windows uses the ANSI code page; output must not crash."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "AGENT_ROUTER_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, "-c", "from agent_router.cli import main; main()", "init"],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "Gateway home directory" in result.stdout.decode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="the NUL device exists only on Windows")
def test_cli_output_survives_nul_device(tmp_path) -> None:
    """NUL is a character device: isatty() is True, yet the encoding is still ANSI."""
    import os
    import subprocess

    env = {**os.environ, "AGENT_ROUTER_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    with open(os.devnull, "w") as nul:
        result = subprocess.run(
            [sys.executable, "-c", "from agent_router.cli import main; main()", "init"],
            stdout=nul,
            stderr=subprocess.PIPE,
            env=env,
            timeout=60,
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


class SnapshotGateway:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot

    def get(self, path: str, **params) -> dict:
        assert path == "/api/v1/snapshot"
        return self.snapshot


def _window(window_id: str, note: str | None) -> dict:
    return {
        "id": window_id,
        "used": 0.4,
        "expected": 0.5,
        "resets_at": None,
        "source": "manual",
        "note": note,
    }


def test_status_prints_window_note(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    note = "counter: model weights changed; recalibrate the pool"
    snapshot = {
        "pools": [
            {
                "id": "opencode-go",
                "title": "OpenCode Go",
                "windows": [_window("5h", None), _window("month", note)],
            }
        ]
    }
    monkeypatch.setattr(
        cli_common, "make_client", lambda base_url, token: SnapshotGateway(snapshot)
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 4  # pool, two windows, and one note line
    month = next(index for index, line in enumerate(lines) if "month:" in line)
    assert lines[month + 1].strip() == f"note: {note}"
    assert "note" not in lines[month - 1]


def _status_lines(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, pool: dict) -> list[str]:
    snapshot = {"pools": [{"id": "claude-pro", "title": "Claude Pro", **pool}]}
    monkeypatch.setattr(
        cli_common, "make_client", lambda base_url, token: SnapshotGateway(snapshot)
    )
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    return result.output.splitlines()


def _role_window(window_id: str, role: str, source: str | None, **fields) -> dict:
    used = None if source is None else 0.4
    expected = 0.5 if role == "pace" else None
    window = {"id": window_id, "role": role, "used": used, "expected": expected}
    window.update(resets_at=None, source=source, note=None, blocked=False)
    window.update(fields)
    return window


def test_status_shows_expected_only_for_the_pace_window(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    windows = [_role_window("5h", "guard", "manual"), _role_window("week", "pace", "manual")]

    lines = _status_lines(runner, monkeypatch, {"windows": windows})

    guard = next(line for line in lines if line.strip().startswith("5h:"))
    pace = next(line for line in lines if line.strip().startswith("week:"))
    assert "expected" not in guard
    assert "expected 50.0%" in pace


def test_status_hint_names_the_pace_window_while_it_has_no_data(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    windows = [_role_window("5h", "guard", "manual"), _role_window("week", "pace", None)]

    lines = _status_lines(runner, monkeypatch, {"windows": windows})

    assert "  calibrate: agent-router calibrate claude-pro week <percent>" in lines


def test_status_has_no_hint_once_the_pace_window_has_data(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    windows = [_role_window("5h", "guard", None), _role_window("week", "pace", "manual")]

    lines = _status_lines(runner, monkeypatch, {"windows": windows})

    assert not any("calibrate:" in line for line in lines)
    assert "not calibrated" not in lines[0]


def test_status_shows_an_exhausted_window_and_the_pool_exclusion(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    until = "2026-11-15T00:00:00+00:00"
    windows = [
        _role_window("5h", "guard", "manual", used=1.0, blocked=True, resets_at=until),
        _role_window("week", "pace", "manual"),
    ]
    pool = {"windows": windows, "blocked_by": "5h", "blocked_until": until, "block": None}

    lines = _status_lines(runner, monkeypatch, pool)

    guard = next(line for line in lines if line.strip().startswith("5h:"))
    assert guard.endswith("; exhausted")
    assert f"  excluded: window 5h exhausted until {format_local(until)}" in lines


def test_status_shows_a_pool_block(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    until = "2026-11-15T00:00:00+00:00"
    windows = [_role_window("5h", "guard", "manual"), _role_window("week", "pace", "manual")]
    block = {"until": until, "reason": "limit reported by the harness"}

    lines = _status_lines(runner, monkeypatch, {"windows": windows, "block": block})

    expected = f"  closed until {format_local(until)}: limit reported by the harness"
    assert expected in lines
    assert "  unblock early: agent-router unblock claude-pro" in lines


@pytest.mark.parametrize(
    ("jobs", "counted"),
    [(3, "3 jobs, 12.5 min, 45200 tokens"), (1, "1 job, 12.5 min, 45200 tokens")],
)
def test_status_shows_gateway_jobs_since_the_last_observation(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens, jobs, counted
) -> None:
    windows = [_role_window("week", "pace", "manual")]
    via = {"since": None, "jobs": jobs, "minutes": 12.5, "requests": 0, "tokens": 45200}

    lines = _status_lines(runner, monkeypatch, {"windows": windows, "via_gateway": via})

    assert f"  via gateway since the last observation: {counted}" in lines


def test_status_hides_gateway_jobs_when_there_are_none(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    windows = [_role_window("week", "pace", "manual")]
    via = {"since": None, "jobs": 0, "minutes": 0.0, "requests": 0, "tokens": 0}

    lines = _status_lines(runner, monkeypatch, {"windows": windows, "via_gateway": via})

    assert not any("via gateway" in line for line in lines)


def test_calibrate_accepts_a_dotted_date(
    runner: CliRunner, test_gateway: None, client, tokens
) -> None:
    result = runner.invoke(
        cli.app, ["calibrate", "claude-pro", "week", "42", "--resets-at", "15.11.2026 14:00"]
    )

    assert result.exit_code == 0, result.output
    snapshot = GatewayClient(BASE_URL, tokens.client, client=client).get("/api/v1/snapshot")
    pool = next(pool for pool in snapshot["pools"] if pool["id"] == "claude-pro")
    window = next(window for window in pool["windows"] if window["id"] == "week")
    moment = datetime.fromisoformat(window["resets_at"])
    assert moment == datetime(2026, 11, 15, 14).astimezone()


def test_calibrate_rejects_an_unknown_date_format(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(
        cli.app, ["calibrate", "claude-pro", "week", "42", "--resets-at", "next monday"]
    )

    assert result.exit_code == 1
    assert "invalid reset time: expected a date such as 2026-12-01 or 01.12.2026" in result.output


@pytest.mark.parametrize("percent", ["150", "-5", "nan", "inf"])
def test_cli_calibrate_rejects_percent_out_of_range(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, percent: str
) -> None:
    sent: list[object] = []
    monkeypatch.setattr(cli, "_gateway", lambda **kwargs: sent.append(kwargs) or None)

    result = runner.invoke(cli.app, ["calibrate", "claude-pro", "week", "--", percent])

    assert result.exit_code == 1
    assert "percent must be between 0 and 100" in result.output
    assert sent == []


def test_cli_calibrate_prints_reset(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(
        cli.app,
        ["calibrate", "claude-pro", "week", "42", "--resets-at", "2026-11-15T00:00:00+00:00"],
    )

    assert result.exit_code == 0, result.output
    expected = format_local(datetime(2026, 11, 15, tzinfo=UTC))
    assert f"Recorded: claude-pro / week, 42%, resets {expected}" in result.output


def test_cli_calibrate_prints_the_kept_reset(runner: CliRunner, test_gateway: None) -> None:
    first = ["calibrate", "claude-pro", "week", "42", "--resets-at", "2026-11-15T00:00:00+00:00"]
    assert runner.invoke(cli.app, first).exit_code == 0
    result = runner.invoke(cli.app, ["calibrate", "claude-pro", "week", "50"])

    assert result.exit_code == 0, result.output
    expected = format_local(datetime(2026, 11, 15, tzinfo=UTC))
    assert f"Recorded: claude-pro / week, 50%, resets {expected}" in result.output


def test_cli_calibrate_without_reset_prints_no_tail(runner: CliRunner, test_gateway: None) -> None:
    result = runner.invoke(cli.app, ["calibrate", "kimi-code", "5h", "30"])

    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("Recorded"))
    assert line == "Recorded: kimi-code / 5h, 30%"


@pytest.mark.parametrize("command", [["serve"], ["status"], ["doctor"], ["jobs"]])
def test_commands_before_init_point_to_init(tmp_path, monkeypatch, command):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 1
    assert result.output == f"no config at {home / 'config.toml'}: run agent-router init first\n"


def test_advice_sends_prefer_routes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, config, tokens
) -> None:
    gateway = AdviceGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: gateway)

    result = runner.invoke(cli.app, ["advice", "--prefer-route", "kimi-cli/k3.1-pro"])

    assert result.exit_code == 0
    assert gateway.params["prefer_routes"] == ["kimi-cli/k3.1-pro"]
