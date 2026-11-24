"""Harness diagnostics and live probes through a mocked gateway."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_router import cli, cli_common, registry
from agent_router import runner as runner_module
from agent_router.client import GatewayError, GatewayUnavailable
from agent_router.config_model import PoolUsage
from agent_router.registry import CounterSpec
from agent_router.runner import ResolvedExecutable
from agent_router.usage import UsageSourceSpec
from helpers import full_config_text, write_config


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.statuses: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.cancel_statuses: dict[str, int] = {}
        self.statuses_after_cancel: dict[str, str] = {}
        self.cancel_unavailable: set[str] = set()
        self.snapshot: dict[str, Any] = {"pools": []}

    def get(self, path: str, **params) -> dict:
        self.calls.append(("GET", path, params))
        if path == "/api/v1/events/stats":
            return {"seq": 0, "subscribers": {}}
        if path == "/api/v1/snapshot":
            return self.snapshot
        job_id = path.removeprefix("/api/v1/jobs/").removesuffix("/wait")
        job = {
            "id": job_id,
            "status": self.statuses.get(job_id, "completed"),
            "route": job_id,
        }
        if job_id in self.errors:
            job["error"] = {"code": self.errors[job_id]}
        return job

    def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", path, body))
        if path == "/api/v1/jobs":
            assert body is not None
            job_id = body["route"]
            return {"job_id": job_id, "status": "queued"}
        job_id = path.removeprefix("/api/v1/jobs/").removesuffix("/cancel")
        assert path == f"/api/v1/jobs/{job_id}/cancel"
        if job_id in self.cancel_unavailable:
            raise GatewayUnavailable("no connection")
        if job_id in self.cancel_statuses:
            if job_id in self.statuses_after_cancel:
                self.statuses[job_id] = self.statuses_after_cancel[job_id]
            status = self.cancel_statuses[job_id]
            raise GatewayError(status, "cancel_failed", f"cancel returned HTTP {status}")
        return {"id": job_id, "status": "cancelled"}


@pytest.fixture(autouse=True)
def isolated_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, config, tokens) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: fake)
    return fake


@pytest.fixture
def fake_harnesses(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = {
        "claude": ResolvedExecutable(None, False),
        "kimi": ResolvedExecutable(Path("kimi.cmd"), True),
        "opencode": ResolvedExecutable(Path("opencode"), False),
    }
    versions = {"kimi": "kimi 2.0.0", "opencode": "opencode 3.0.0"}
    monkeypatch.setattr(
        cli,
        "resolve_executable",
        lambda name: resolved.get(name, ResolvedExecutable(None, False)),
        raising=False,
    )
    monkeypatch.setattr(
        cli, "_harness_version", lambda exe: versions.get(Path(str(exe.path)).stem), raising=False
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: None)


def test_doctor_table_lists_three_harnesses_with_details(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None
) -> None:
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Harness" in result.stdout
    assert "Enabled routes" in result.stdout
    assert "claude" in result.stdout
    assert "kimi.cmd" in result.stdout
    assert "shim" in result.stdout
    assert "kimi 2.0.0" in result.stdout
    assert "kimi-cli/k3.1-pro" in result.stdout
    assert "kimi-cli/kimi-for-coding" in result.stdout
    assert "opencode" in result.stdout
    assert "executable" in result.stdout
    assert "opencode 3.0.0" in result.stdout
    assert "opencode-cli/glm-5.3-flash" in result.stdout


def test_doctor_warns_about_empty_allowed_roots(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None
) -> None:
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Warning: tasks.allowed_roots is empty" in result.stdout


def test_doctor_warns_about_enabled_route_with_missing_harness(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    write_config(home, full_config_text(confirm=False).replace("enabled = false", "enabled = true"))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    warnings = [line for line in result.stdout.splitlines() if "Warning:" in line]
    assert any("claude-cli/sonnet" in line and "not found" in line for line in warnings)


def test_doctor_lists_cline(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "cline | no | — | — | —" in result.stdout.splitlines()

    write_config(home, full_config_text(confirm=False).replace("enabled = false", "enabled = true"))
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    expected = "Warning: route cline/glm-5.3 is enabled, but harness cline not found"
    assert expected in result.stdout.splitlines()


def test_doctor_version_uses_closed_stdin_timeout_and_no_console_window(
    runner: CliRunner, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"test 1.0\n", stderr=b"")

    monkeypatch.setattr(
        cli,
        "resolve_executable",
        lambda name: ResolvedExecutable(Path(name), False),
        raising=False,
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert len(calls) == 4  # claude, cline, kimi, opencode
    for argv, kwargs in calls:
        assert argv[1:] == ["--version"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 10
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_doctor_live_submits_one_read_job_per_harness_and_waits(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    text = full_config_text(confirm=False).replace(
        "allowed_roots = []", f"allowed_roots = [{json.dumps(str(tmp_path))}]"
    )
    text = text.replace("enabled = false", "enabled = true")
    # the first route (claude) asks for confirmation: live probes skip it
    text = text.replace("confirm = false", "confirm = true", 1)
    write_config(home, text)
    gateway.statuses["opencode-cli/glm-5.3-flash"] = "failed"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    posts = [call for call in gateway.calls if call[:2] == ("POST", "/api/v1/jobs")]
    assert [call[2]["route"] for call in posts] == [
        "kimi-cli/k3.1-pro",
        "opencode-cli/glm-5.3-flash",
        "cline/glm-5.3",
    ]
    assert all(call[2]["mode"] == "read" for call in posts)
    assert all(call[2]["cwd"] == str(tmp_path) for call in posts)
    assert all(call[2]["prompt"] for call in posts)
    waits = [call for call in gateway.calls if call[0] == "GET" and call[1].endswith("/wait")]
    assert [call[1] for call in waits] == [
        "/api/v1/jobs/kimi-cli/k3.1-pro/wait",
        "/api/v1/jobs/opencode-cli/glm-5.3-flash/wait",
        "/api/v1/jobs/cline/glm-5.3/wait",
    ]
    assert all(call[2] == {"timeout": 100} for call in waits)
    assert "kimi-cli/k3.1-pro" in result.stdout
    assert "completed" in result.stdout
    assert "opencode-cli/glm-5.3-flash" in result.stdout
    # no error code in the response, so the line has no parentheses
    assert "Live opencode-cli/glm-5.3-flash: failed\n" in result.stdout


def _enable_live_probes(home: Path, tmp_path: Path) -> None:
    text = full_config_text(confirm=False).replace(
        "allowed_roots = []", f"allowed_roots = [{json.dumps(str(tmp_path))}]"
    )
    write_config(home, text.replace("enabled = false", "enabled = true"))


def test_doctor_live_skips_probes_when_allowed_roots_empty(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None
) -> None:
    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 0
    assert "Warning: allowed_roots is empty — live probes skipped" in result.stdout
    assert ("POST", "/api/v1/jobs") not in [call[:2] for call in gateway.calls]


def test_doctor_live_without_probes_fails(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path, tmp_path: Path
) -> None:
    # allowed_roots is set, but every route is disabled, so there is nothing to probe
    text = full_config_text(confirm=False).replace(
        "allowed_roots = []", f"allowed_roots = [{json.dumps(str(tmp_path))}]"
    )
    write_config(home, text.replace("enabled = true", "enabled = false"))

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code != 0
    assert "no routes to probe" in result.output
    assert ("POST", "/api/v1/jobs") not in [call[:2] for call in gateway.calls]


def test_doctor_live_failed_probe_reports_error_code(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    gateway.statuses["opencode-cli/glm-5.3-flash"] = "failed"
    gateway.errors["opencode-cli/glm-5.3-flash"] = "bad_route"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    assert "Live kimi-cli/k3.1-pro: completed" in result.stdout
    assert "Live opencode-cli/glm-5.3-flash: failed (bad_route)" in result.stdout


def test_doctor_live_running_probe_is_cancelled(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    gateway.statuses["opencode-cli/glm-5.3-flash"] = "running"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    assert (
        "POST",
        "/api/v1/jobs/opencode-cli/glm-5.3-flash/cancel",
        None,
    ) in gateway.calls
    expected = "Live opencode-cli/glm-5.3-flash: did not complete within 100 s — cancelled"
    assert expected in result.stdout


def test_doctor_live_all_completed_exits_zero(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 0
    assert "Live kimi-cli/k3.1-pro: completed" in result.stdout
    assert "Live opencode-cli/glm-5.3-flash: completed" in result.stdout


def test_doctor_live_cancel_conflict_reports_final_status(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    route = "kimi-cli/k3.1-pro"
    gateway.statuses[route] = "running"
    gateway.cancel_statuses[route] = 409
    gateway.statuses_after_cancel[route] = "completed"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 0
    assert f"Live {route}: completed" in result.stdout
    assert "Live opencode-cli/glm-5.3-flash: completed" in result.stdout
    assert ("GET", f"/api/v1/jobs/{route}", {}) in gateway.calls


def test_doctor_live_cancel_conflict_with_running_job_fails(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    route = "kimi-cli/k3.1-pro"
    gateway.statuses[route] = "running"
    gateway.cancel_statuses[route] = 409
    gateway.statuses_after_cancel[route] = "running"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    assert f"Live {route}: running — cancel failed, job still running" in result.stdout


def test_doctor_live_awaiting_approval_line(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    route = "opencode-cli/glm-5.3-flash"
    gateway.statuses[route] = "awaiting_approval"

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    assert f"Live {route}: awaiting approval — cancelled" in result.stdout


def test_doctor_live_other_gateway_error_still_fails(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    route = "opencode-cli/glm-5.3-flash"
    gateway.statuses[route] = "running"
    gateway.cancel_statuses[route] = 500

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 1
    assert "request denied: cancel returned HTTP 500" in result.output


def test_doctor_live_cancel_without_gateway_is_clear_error(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
    tmp_path: Path,
) -> None:
    _enable_live_probes(home, tmp_path)
    route = "kimi-cli/k3.1-pro"
    gateway.statuses[route] = "running"
    gateway.cancel_unavailable.add(route)

    result = runner.invoke(cli.app, ["doctor", "--live"])

    assert result.exit_code == 5
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "gateway unavailable" in result.output


def _weighted_config(home: Path, *, limits: str, prices: str | None) -> None:
    lines = 'limit_base_usd = 180\nmodel_limits_date = "2026-11-16"\n'
    if prices is not None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "prices.toml").write_text(prices, encoding="utf-8")
        lines += 'price_file = "prices.toml"\n'
    text = full_config_text(confirm=False).replace(
        'counter_provider = "opencode-go"\n',
        f'counter_provider = "opencode-go"\n{lines}\n[pools.model_limits]\n{limits}',
    )
    write_config(home, text)


def test_doctor_warns_about_model_without_limit(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    _weighted_config(home, limits='"glm-5.3" = 120\n', prices=None)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    warnings = [line for line in result.stdout.splitlines() if "Warning:" in line]
    assert any("opencode-cli/glm-5.3-flash" in line and "model_limits" in line for line in warnings)


def test_doctor_silent_when_every_model_has_limit_and_price(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    _weighted_config(
        home,
        limits='"glm-5.3-flash" = 180\n"glm-5.3" = 120\n',
        prices='"glm-5.3-flash" = [0.1, 0.5]\n"glm-5.3" = [1.0, 4.0]\n',
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    warnings = [line for line in result.stdout.splitlines() if "Warning:" in line]
    assert not any("model_limits" in line or "no price" in line for line in warnings)


def test_doctor_warns_about_route_without_price(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    _weighted_config(home, limits='"glm-5.3-flash" = 180\n', prices='"glm-5.3" = [1.0, 4.0]\n')

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    warnings = [line for line in result.stdout.splitlines() if "Warning:" in line]
    assert any("opencode-cli/glm-5.3-flash" in line and "no price" in line for line in warnings)


def test_doctor_shows_model_limits_date_age(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None, home: Path
) -> None:
    _weighted_config(home, limits='"glm-5.3-flash" = 180\n', prices=None)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if "2026-11-16" in line]
    assert lines and "opencode-go" in lines[0] and "days ago" in lines[0]


class FakeCounter:
    def __init__(self, config, store, clock, bus) -> None:
        pass

    def sample_once(self) -> None:
        pass


def _register_counter(monkeypatch: pytest.MonkeyPatch, name: str, sees: frozenset[str]) -> None:
    monkeypatch.setitem(registry._COUNTERS, name, CounterSpec(name, FakeCounter, sees))


def _counter_config_text(counter: str) -> str:
    return full_config_text(confirm=False).replace(
        'counter = "opencode_stats"', f'counter = "{counter}"'
    )


def _counter_config(home: Path, counter: str) -> None:
    write_config(home, _counter_config_text(counter))


def _write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def _edit_route(text: str, route_id: str, old: str, new: str) -> str:
    start = text.index(f'id = "{route_id}"')
    block_start = text.rindex("[[routes]]", 0, start)
    block_end = text.find("\n[[routes]]", start)
    block_end = len(text) if block_end == -1 else block_end
    block = text[block_start:block_end]
    assert old in block
    return text[:block_start] + block.replace(old, new, 1) + text[block_end:]


def _warnings(result) -> list[str]:
    return [line for line in result.stdout.splitlines() if "Warning:" in line]


def test_doctor_warns_when_pool_counter_cannot_see_route(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "blind", frozenset())
    _counter_config(home, "blind")

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert any(
        "opencode-cli/glm-5.3-flash" in line
        and "counter blind" in line
        and "does not see harness opencode" in line
        and 'spend = "journal"' in line
        for line in _warnings(result)
    )


def test_doctor_quiet_when_route_sets_journal(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "blind", frozenset())
    text = (
        full_config_text(confirm=False)
        .replace('counter = "opencode_stats"', 'counter = "blind"')
        .replace('backend = "opencode"', 'backend = "opencode"\nspend = "journal"')
    )
    write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("does not see" in line for line in _warnings(result))


def test_doctor_quiet_when_counter_sees_route(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "sees-opencode", frozenset({"opencode"}))
    _counter_config(home, "sees-opencode")

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("does not see" in line for line in _warnings(result))


def test_doctor_warns_when_pool_has_no_counter(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
) -> None:
    text = full_config_text(confirm=False).replace('counter = "opencode_stats"\n', "")
    write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert any(
        "route opencode-cli/glm-5.3-flash:" in line
        and "pool opencode-go has no counter" in line
        and 'spend = "journal"' in line
        for line in _warnings(result)
    )


def test_doctor_quiet_when_explicit_counter_sees_harness(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "sees-opencode", frozenset({"opencode"}))
    text = _edit_route(
        full_config_text(confirm=False).replace(
            'counter = "opencode_stats"', 'counter = "sees-opencode"'
        ),
        "opencode-cli/glm-5.3-flash",
        'backend = "opencode"',
        'backend = "opencode"\nspend = "counter"',
    )
    write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("does not see" in line for line in _warnings(result))


def test_doctor_skips_disabled_routes(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "blind", frozenset())
    text = _edit_route(
        _counter_config_text("blind"),
        "opencode-cli/glm-5.3",
        "enabled = true",
        "enabled = false",
    )
    write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    warnings = [line for line in _warnings(result) if "does not see" in line]
    assert len(warnings) == 1
    assert "route opencode-cli/glm-5.3-flash:" in warnings[0]


def test_doctor_rejects_explicit_counter_when_counter_cannot_see(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_counter(monkeypatch, "blind", frozenset())
    text = _edit_route(
        _counter_config_text("blind"),
        "opencode-cli/glm-5.3-flash",
        'backend = "opencode"',
        'backend = "opencode"\nspend = "counter"',
    )
    _write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "counter blind does not see harness opencode" in result.output


def test_doctor_rejects_unknown_harness(runner: CliRunner, home: Path) -> None:
    text = full_config_text(confirm=False).replace(
        '[backends.opencode]\nexecutable = ""',
        '[backends.opencode]\nharness = "nope"\nexecutable = ""',
    )
    _write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "unknown harness" in result.output


# --- usage sources: env variables, read health, mode without a source -------------------


def _register_usage_source(monkeypatch: pytest.MonkeyPatch, default_env: str | None = None) -> None:
    spec = UsageSourceSpec(
        name="fake-usage",
        factory=lambda pool_usage, env: None,
        windows=frozenset({"monthly"}),
        observation_source="fake_usage",
        default_api_key_env=default_env,
    )
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)


def _with_pool_usage(text: str, body: str) -> str:
    """Config text with a [pools.usage] sub-table on the last pool, before the first route."""
    return text.replace("[[routes]]", f"[pools.usage]\n{body}\n\n[[routes]]", 1)


_POOL_WITH_SOURCE = (
    'source = "fake-usage"\napi_key_env = "FAKE_USAGE_KEY"\nwindows = { monthly = "month" }'
)


def test_doctor_warns_when_usage_api_key_env_is_not_set(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_usage_source(monkeypatch)
    monkeypatch.delenv("FAKE_USAGE_KEY", raising=False)
    write_config(home, _with_pool_usage(full_config_text(confirm=False), _POOL_WITH_SOURCE))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (
        "Warning: pool opencode-go: usage source fake-usage needs environment variable "
        "FAKE_USAGE_KEY, which is not set" in _warnings(result)
    )


def test_doctor_quiet_when_usage_api_key_env_is_set(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_usage_source(monkeypatch)
    # a fake value: the check looks at presence only and never prints what it finds
    monkeypatch.setenv("FAKE_USAGE_KEY", "test-value")
    write_config(home, _with_pool_usage(full_config_text(confirm=False), _POOL_WITH_SOURCE))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("needs environment variable" in line for line in _warnings(result))


def test_doctor_names_the_sources_default_key_variable(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _register_usage_source(monkeypatch, default_env="FAKE_DEFAULT_KEY")
    monkeypatch.delenv("FAKE_DEFAULT_KEY", raising=False)
    body = 'source = "fake-usage"\nwindows = { monthly = "month" }'
    write_config(home, _with_pool_usage(full_config_text(confirm=False), body))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (
        "Warning: pool opencode-go: usage source fake-usage needs environment variable "
        "FAKE_DEFAULT_KEY, which is not set" in _warnings(result)
    )


def test_doctor_prints_the_sources_own_check(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    seen: list[PoolUsage] = []

    def check(pool_usage: PoolUsage, env) -> str | None:
        seen.append(pool_usage)
        return "the CLI is not signed in"

    spec = UsageSourceSpec(
        name="fake-usage",
        factory=lambda pool_usage, env: None,
        windows=frozenset({"monthly"}),
        observation_source="fake_usage",
        check=check,
    )
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)
    body = 'source = "fake-usage"\nexecutable = "fake-cli"\nwindows = { monthly = "month" }'
    write_config(home, _with_pool_usage(full_config_text(confirm=False), body))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (
        "Warning: pool opencode-go: usage source fake-usage: the CLI is not signed in"
        in _warnings(result)
    )
    assert [item.executable for item in seen] == ["fake-cli"]


def _snapshot_pool_usage(**changes) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source": "fake-usage",
        "mode": "auto",
        "last_attempt_at": "2026-11-13T09:12:00+00:00",
        "last_success_at": "2026-11-13T09:00:00+00:00",
        "last_error": None,
        "age_s": 7200.0,
        "stale": False,
    }
    values.update(changes)
    return {"pools": [{"id": "opencode-go", "usage": values}]}


def _write_source_config(monkeypatch: pytest.MonkeyPatch, home: Path, gateway: FakeGateway) -> None:
    _register_usage_source(monkeypatch)
    monkeypatch.setenv("FAKE_USAGE_KEY", "test-value")
    write_config(home, _with_pool_usage(full_config_text(confirm=False), _POOL_WITH_SOURCE))
    gateway.snapshot = _snapshot_pool_usage()


def test_doctor_warns_when_the_last_usage_read_failed(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _write_source_config(monkeypatch, home, gateway)
    gateway.snapshot = _snapshot_pool_usage(last_error="unauthorized", age_s=120.0)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Warning: pool opencode-go: last usage read failed: unauthorized" in _warnings(result)
    assert not any("usage data is stale" in line for line in _warnings(result))


def test_doctor_warns_when_usage_data_is_stale(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _write_source_config(monkeypatch, home, gateway)
    gateway.snapshot = _snapshot_pool_usage(stale=True)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (
        "Warning: pool opencode-go: usage data is stale, no successful read for 2 h"
        in _warnings(result)
    )


def test_doctor_warns_when_usage_was_never_read(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _write_source_config(monkeypatch, home, gateway)
    gateway.snapshot = _snapshot_pool_usage(
        last_success_at=None, last_error=None, age_s=None, stale=True
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Warning: pool opencode-go: usage data is stale, no successful read yet" in _warnings(
        result
    )


def test_doctor_warns_when_pool_mode_is_auto_without_a_source(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
) -> None:
    write_config(home, _with_pool_usage(full_config_text(confirm=False), 'mode = "auto"'))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    usage_warnings = [line for line in _warnings(result) if "usage mode is auto" in line]
    assert usage_warnings == ["Warning: pool opencode-go: usage mode is auto but no source is set"]


def test_doctor_warns_for_every_pool_when_usage_table_is_present(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    home: Path,
) -> None:
    text = '[usage]\ninterval = "1h"\n\n' + full_config_text(confirm=False)
    write_config(home, text)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    mode_warnings = [line for line in _warnings(result) if "usage mode is auto" in line]
    assert [line.split(":")[1].strip() for line in mode_warnings] == [
        "pool claude-pro",
        "pool kimi-code",
        "pool opencode-go",
    ]


def test_doctor_is_quiet_about_usage_when_the_config_never_mentions_it(
    runner: CliRunner, gateway: FakeGateway, fake_harnesses: None
) -> None:
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("usage" in line for line in _warnings(result))
    assert ("GET", "/api/v1/snapshot", {}) not in [call[:3] for call in gateway.calls]


def test_doctor_warns_about_missing_usage_fields_and_format_change(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _write_source_config(monkeypatch, home, gateway)
    gateway.snapshot = _snapshot_pool_usage(missing_fields=["monthly"], format_changed=True)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    hint = 'update agent-router or switch the pool to mode = "manual" and calibrate'
    assert (
        "Warning: pool opencode-go: usage source fake-usage no longer reports monthly; "
        f"window month falls back to the journal; {hint}" in _warnings(result)
    )
    assert (
        "Warning: pool opencode-go: usage source fake-usage response format may have changed; "
        f"{hint}" in _warnings(result)
    )


def test_doctor_hides_usage_drift_warnings_in_manual_mode(
    runner: CliRunner,
    gateway: FakeGateway,
    fake_harnesses: None,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    _write_source_config(monkeypatch, home, gateway)
    gateway.snapshot = _snapshot_pool_usage(
        mode="manual", missing_fields=["monthly"], format_changed=True
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert not any("no longer reports" in line for line in _warnings(result))
    assert not any("response format may have changed" in line for line in _warnings(result))
