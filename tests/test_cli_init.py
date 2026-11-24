"""First run: init options, next steps, calibration hints and the example configs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router import cli, cli_common, cli_gateway
from agent_router.auth import ensure_tokens
from agent_router.client import GatewayClient
from agent_router.config import load_config

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"


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


def _roots(home: Path) -> list[Path]:
    return [Path(root) for root in load_config(home / "config.toml").tasks.allowed_roots]


def test_init_allowed_root_writes_tasks(home: Path, runner: CliRunner, tmp_path: Path) -> None:
    first, second = tmp_path / "repo one", tmp_path / "репо"  # non-ascii on purpose
    first.mkdir()
    second.mkdir()

    result = runner.invoke(
        cli.app, ["init", "--allowed-root", str(first), "--allowed-root", str(second)]
    )

    assert result.exit_code == 0, result.output
    assert _roots(home) == [first.resolve(), second.resolve()]


def test_init_allowed_root_adds_to_existing_config(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    tokens = ensure_tokens(home)

    once = runner.invoke(cli.app, ["init", "--allowed-root", str(repo)])
    twice = runner.invoke(cli.app, ["init", "--allowed-root", str(repo)])

    assert once.exit_code == 0, once.output
    assert twice.exit_code == 0, twice.output
    assert _roots(home) == [repo.resolve()]
    assert ensure_tokens(home) == tokens


def test_init_allowed_root_replaces_config_whole_or_not_at_all(
    home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_path = home / "config.toml"
    before = config_path.read_bytes()
    names = sorted(path.name for path in home.iterdir())

    def locked(source, target) -> None:
        raise PermissionError("file is locked")

    monkeypatch.setattr(cli_gateway.os, "replace", locked)
    result = runner.invoke(cli.app, ["init", "--allowed-root", str(repo)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"cannot update {config_path}: file is locked" in result.output
    assert config_path.read_bytes() == before
    assert sorted(path.name for path in home.iterdir()) == names


def test_init_allowed_root_keeps_existing_roots(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    assert runner.invoke(cli.app, ["init", "--allowed-root", str(old)]).exit_code == 0

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(new)])

    assert result.exit_code == 0, result.output
    assert _roots(home) == [old.resolve(), new.resolve()]


def test_init_allowed_root_must_be_a_directory(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(missing)])

    assert result.exit_code == 1
    assert "not a directory" in result.output
    assert not (home / "config.toml").exists()


def test_init_without_allowed_root_prints_the_command(
    home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0
    assert _roots(home) == []
    assert "agent-router init --allowed-root" in result.output
    assert str(tmp_path) in result.output


def test_init_with_roots_does_not_print_the_command(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(cli.app, ["init", "--allowed-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "agent-router init --allowed-root" not in result.output


def test_init_prints_next_steps_with_the_configured_port(
    home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_gateway, "_found_harnesses", lambda: ["kimi"])
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_path = home / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("port = 8787", "port = 9123"), encoding="utf-8")

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(tmp_path)])

    assert result.exit_code == 0
    steps = result.output.split("Next steps", 1)[1]
    assert "agent-router serve" in steps
    assert "agent-router run --no-wait" in steps
    assert "agent-router approve" in steps
    assert "http://127.0.0.1:9123/" in steps
    assert "8787" not in steps


def test_init_with_broken_config_reports_the_error_and_paths(home: Path, runner: CliRunner) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text("[server\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 1
    assert str(home / "config.toml") in result.output
    assert "config.toml is invalid:" in result.output
    assert "Next steps" not in result.output


def test_status_marks_pools_without_data(runner: CliRunner, test_gateway: None) -> None:
    before = runner.invoke(cli.app, ["status"])
    calibrated = runner.invoke(cli.app, ["calibrate", "claude-pro", "week", "42"])
    after = runner.invoke(cli.app, ["status"])

    assert before.exit_code == 0
    assert calibrated.exit_code == 0
    assert after.exit_code == 0
    header = next(line for line in before.output.splitlines() if line.startswith("claude-pro"))
    assert "not calibrated" in header
    assert "agent-router calibrate claude-pro" in before.output
    header = next(line for line in after.output.splitlines() if line.startswith("claude-pro"))
    assert "not calibrated" not in header


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.toml")), ids=lambda path: path.name)
def test_example_config_loads(path: Path) -> None:
    config = load_config(path)

    assert config.pools
    assert any(route.enabled for route in config.routes)


def test_examples_cover_several_setups() -> None:
    names = sorted(path.name for path in EXAMPLES.glob("*.toml"))

    assert len(names) >= 3
    for path in EXAMPLES.glob("*.toml"):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#"), f"{path.name}: starts with a comment that names the setup"


def test_init_allowed_root_matches_existing_roots_by_resolved_path(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_path = home / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    spelled = str(repo).replace("\\", "\\\\") + ("\\\\" if "\\" in str(repo) else "/")
    config_path.write_text(
        text.replace("allowed_roots = []", f'allowed_roots = ["{spelled}"]'), encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(repo)])

    assert result.exit_code == 0, result.output
    assert _roots(home) == [repo.resolve()]


def test_init_allowed_root_finds_tasks_header_with_spaces_and_comment(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_path = home / "config.toml"
    text = config_path.read_bytes().decode("utf-8")
    text = text.replace("[tasks]\n", "[ tasks ]  # job directories\n", 1)
    config_path.write_bytes(text.encode("utf-8"))

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert _roots(home) == [tmp_path.resolve()]
    updated = config_path.read_bytes().decode("utf-8")
    assert updated.replace(f'["{tmp_path.resolve().as_posix()}"]', "[]") == text


def test_printed_directories_use_forward_slashes() -> None:
    from pathlib import PureWindowsPath

    assert cli_gateway._shell_quote_dir(PureWindowsPath("C:/")) == "C:/"
    quoted = cli_gateway._shell_quote_dir(PureWindowsPath("C:/My Repo"))
    assert "C:/My Repo" in quoted
    assert quoted != "C:/My Repo"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_init_creates_a_private_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    home = tmp_path / "new home"
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert home.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_tokens_create_a_private_home(tmp_path: Path) -> None:
    home = tmp_path / "token home"

    ensure_tokens(home)

    assert home.stat().st_mode & 0o777 == 0o700


# --- first route and hints ------------------------------------------------------------------


def _init_with(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, found: list[str], *args: str
) -> str:
    monkeypatch.setattr(cli_gateway, "_found_harnesses", lambda: found)
    result = runner.invoke(cli.app, ["init", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_template_gives_opencode_a_standard_route(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_with(runner, monkeypatch, ["opencode"])

    routes = load_config(home / "config.toml").routes
    assert any(
        route.backend == "opencode" and route.enabled and "standard" in route.tiers
        for route in routes
    )


def test_template_maps_opencode_caller_to_its_pool(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_with(runner, monkeypatch, ["opencode"])

    assert load_config(home / "config.toml").callers["opencode"] == "opencode-go"


def test_init_without_harnesses_says_what_to_install(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, [])

    assert "no harness found" in output
    assert "run:" not in output


def test_init_names_disabled_routes_of_found_harness(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["cline"])

    line = next(line for line in output.splitlines() if "harness cline" in line)
    assert "routes are disabled" in line
    assert "cline/glm-5.3" in line
    assert "no enabled route: jobs are rejected" in output


def test_init_warns_about_placeholder_usd_limits(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["opencode"])

    assert "limit_usd = 10 is a placeholder" in output


def test_init_found_harness_with_standard_route_has_no_route_warnings(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["kimi"])

    assert "routes are disabled" not in output
    assert "no enabled route" not in output
    assert "placeholder" not in output


def test_init_next_steps_include_advice_and_wait(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))

    steps = output[output.index("Next steps:") :]
    positions = [
        steps.index(needle)
        for needle in (
            "agent-router advice --tier standard",
            "agent-router run",
            "agent-router approve",
            "agent-router wait <job-id>",
        )
    ]
    assert positions == sorted(positions)


def test_init_hints_also_check_an_existing_config(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["cline"])
    repo = tmp_path / "repo"
    repo.mkdir()

    output = _init_with(runner, monkeypatch, ["cline"], "--allowed-root", str(repo))

    assert "harness cline: routes are disabled" in output


def test_init_without_harnesses_says_how_to_add_a_route_later(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, [])

    line = next(line for line in output.splitlines() if "no harness found" in line)
    assert "uncomment" in line
    assert "run init again" not in output


def test_init_after_installing_a_harness_names_its_commented_routes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_with(runner, monkeypatch, [])

    output = _init_with(runner, monkeypatch, ["kimi"])

    line = next(line for line in output.splitlines() if "harness kimi" in line)
    assert "no routes in config.toml" in line
    assert "uncomment" in line
    assert "--tier cheap" not in output
    assert "approve:" not in output


def test_init_hides_job_steps_without_an_enabled_route(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["cline"])

    steps = output[output.index("Next steps:") :]
    assert "serve:" in steps
    assert "run:" not in steps
    assert "approve:" not in steps


def test_init_adding_a_root_to_an_existing_config_says_to_restart_serve(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(repo))
    other = tmp_path / "other"
    other.mkdir()

    second = _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(other))

    assert "restart agent-router serve" not in first
    assert "restart agent-router serve" in second


def _edit_route(home: Path, route_id: str, old: str, new: str) -> None:
    path = home / "config.toml"
    text = path.read_text(encoding="utf-8")
    start = text.index(f'id = "{route_id}"')
    end = text.index("\n\n", start)
    block = text[start:end]
    assert old in block
    path.write_text(text[:start] + block.replace(old, new) + text[end:], encoding="utf-8")


def test_init_without_root_hides_job_steps(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["kimi"])

    steps = output[output.index("Next steps:") :]
    assert "serve:" in steps
    assert "run:" not in steps
    assert "approve:" not in steps


def test_init_without_harnesses_mentions_enabling_and_no_route(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, [])

    line = next(line for line in output.splitlines() if "no harness found" in line)
    assert "enabled = true" in line
    assert "no enabled route: jobs are rejected" in output


def test_init_suggests_the_tier_that_enabled_routes_serve(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["claude"], "--allowed-root", str(tmp_path))
    _edit_route(home, "claude-cli/sonnet", 'tiers = ["standard", "strong"]', 'tiers = ["strong"]')

    output = _init_with(runner, monkeypatch, ["claude"])

    assert "--tier cheap" not in output
    assert "run jobs with --tier strong" in output
    steps = output[output.index("Next steps:") :]
    assert "agent-router advice --tier strong" in steps
    assert "agent-router run --no-wait --tier strong" in steps
    assert "approve:" in steps


def test_init_skips_approve_when_no_route_needs_confirmation(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))
    for route_id in ("kimi-cli/k3.1-pro", "kimi-cli/kimi-for-coding"):
        _edit_route(home, route_id, "confirm = true", "confirm = false")

    output = _init_with(runner, monkeypatch, ["kimi"])

    steps = output[output.index("Next steps:") :]
    assert "run:" in steps
    assert "approve:" not in steps


def test_init_counts_a_harness_set_by_executable(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, [])
    executable = tmp_path / "kimi.exe"
    executable.write_bytes(b"")
    path = home / "config.toml"
    text = path.read_text(encoding="utf-8")
    old = '[backends.kimi]\nexecutable = ""'
    assert old in text
    path.write_text(
        text.replace(old, f'[backends.kimi]\nexecutable = "{executable.as_posix()}"'),
        encoding="utf-8",
    )

    output = _init_with(runner, monkeypatch, [])

    assert "no harness found" not in output
    assert "Harnesses found: kimi" in output
    assert "harness kimi: no routes in config.toml" in output


def test_init_warns_about_an_enabled_route_without_its_harness(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))

    output = _init_with(runner, monkeypatch, [])

    line = next(line for line in output.splitlines() if "route kimi-cli/k3.1-pro" in line)
    assert "harness kimi was not found" in line
    assert "[backends.kimi] executable" in line


def test_init_allowed_root_keeps_the_inline_comment(
    home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_path = home / "config.toml"
    text = config_path.read_bytes().decode("utf-8")
    assert "allowed_roots = []" in text
    existing_root = (tmp_path / "a#b").resolve().as_posix()
    text = text.replace("allowed_roots = []", f'allowed_roots = ["{existing_root}"]  # my repos', 1)
    config_path.write_bytes(text.encode("utf-8"))

    result = runner.invoke(cli.app, ["init", "--allowed-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    updated = config_path.read_bytes().decode("utf-8")
    root = tmp_path.resolve().as_posix()
    assert f'allowed_roots = ["{existing_root}", "{root}"]  # my repos' in updated.splitlines()


@pytest.mark.parametrize("root", ["//server/share/repo", r"\\server\share\repo"])
def test_init_rejects_a_network_allowed_root(home: Path, runner: CliRunner, root: str) -> None:
    result = runner.invoke(cli.app, ["init", "--allowed-root", root])

    assert result.exit_code == 1
    assert "network and device paths are not accepted" in result.output
    assert not (home / "config.toml").exists()


def test_init_with_a_root_already_allowed_does_not_say_to_restart(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))

    again = _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))

    assert "restart agent-router serve" not in again


def test_init_skips_approve_when_the_read_routes_need_no_confirmation(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))
    _edit_route(home, "kimi-cli/k3.1-pro", "confirm = true", "confirm = false")
    _edit_route(home, "kimi-cli/kimi-for-coding", 'modes = ["read", "write"]', 'modes = ["write"]')

    output = _init_with(runner, monkeypatch, ["kimi"])

    steps = output[output.index("Next steps:") :]
    assert "--mode read" in steps
    assert "approve:" not in steps


def test_init_without_a_read_route_prints_no_first_job(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))
    for route_id in ("kimi-cli/k3.1-pro", "kimi-cli/kimi-for-coding"):
        _edit_route(home, route_id, 'modes = ["read", "write"]', 'modes = ["write"]')

    output = _init_with(runner, monkeypatch, ["kimi"])

    steps = output[output.index("Next steps:") :]
    assert "run:" not in steps
    assert "--mode write" not in output


def test_init_names_the_backend_section_of_a_route_with_a_missing_executable(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_with(runner, monkeypatch, ["kimi"], "--allowed-root", str(tmp_path))
    path = home / "config.toml"
    text = path.read_text(encoding="utf-8")
    old = '[backends.kimi]\nexecutable = ""'
    assert old in text
    missing = (tmp_path / "missing" / "kimi.exe").as_posix()
    path.write_text(
        text.replace(old, f'[backends.kimi]\nexecutable = "{missing}"'), encoding="utf-8"
    )

    output = _init_with(runner, monkeypatch, ["kimi"])

    line = next(line for line in output.splitlines() if "route kimi-cli/k3.1-pro" in line)
    assert "[backends.kimi] executable" in line
    assert missing in line


def test_init_hint_for_a_harness_without_routes_does_not_assume_the_template(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_with(runner, monkeypatch, ["kimi"])

    output = _init_with(runner, monkeypatch, ["kimi", "opencode"])

    line = next(line for line in output.splitlines() if "harness opencode:" in line)
    assert "add or uncomment" in line


def test_init_says_serve_keeps_its_terminal(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _init_with(runner, monkeypatch, ["kimi"])

    line = next(line for line in output.splitlines() if "serve: agent-router serve" in line)
    assert "another terminal" in line
