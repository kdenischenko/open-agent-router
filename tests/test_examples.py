"""Checks that the example configs and the template agree with the documented setups."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_router.config import load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "docs" / "examples").glob("*.toml"))
TEMPLATE = ROOT / "src" / "agent_router" / "config.example.toml"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_systemone_models_use_systemone_kind(path: Path) -> None:
    models = load_config(path).classifier.effective_models
    decision = [model for model in models if model.model.startswith(("tev", "jev", "lfm"))]

    assert all(model.kind == "systemone" for model in decision), path.name


@pytest.mark.parametrize("path", [TEMPLATE, *EXAMPLES], ids=lambda path: path.name)
def test_claude_five_hour_window_is_fixed_everywhere(path: Path) -> None:
    config = load_config(path)
    claude_pools = {route.pool for route in config.routes if route.backend == "claude"}

    for pool in config.pools:
        if pool.id in claude_pools:
            five_hour = [window for window in pool.windows if window.id == "5h"]
            assert five_hour and all(window.kind == "fixed" for window in five_hour), path.name


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_single_pool_examples_say_they_are_for_direct_cli_use(path: Path) -> None:
    if len(load_config(path).pools) > 1:
        pytest.skip("several pools")
    header = path.read_text(encoding="utf-8").split("\n\n", 1)[0].lower()

    assert "direct cli use" in header, path.name


def test_install_explains_how_to_apply_an_example() -> None:
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    paragraphs = [part for part in text.split("\n\n") if "docs/examples/" in part]
    steps = " ".join(paragraphs).lower()

    assert "copy" in steps
    assert "config.toml" in steps
    assert "init --allowed-root" in steps


@pytest.mark.parametrize("path", [TEMPLATE, *EXAMPLES], ids=lambda path: path.name)
def test_statusline_feeds_only_a_claude_pool(path: Path) -> None:
    config = load_config(path)
    if config.statusline is None:
        pytest.skip("no status line")
    claude_pools = {route.pool for route in config.routes if route.backend == "claude"}

    assert config.statusline.pool in claude_pools, path.name


def test_install_snippets_do_not_repeat_a_pool() -> None:
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    snippets = re.findall(r"```toml\n(.*?)```", text, flags=re.DOTALL)
    pool_ids = [
        match
        for snippet in snippets
        for match in re.findall(r'\[\[pools\]\]\s*\n\s*id = "([^"]+)"', snippet)
    ]

    assert len(pool_ids) == len(set(pool_ids)), pool_ids


def test_security_marks_linux_and_macos_experimental() -> None:
    first = (ROOT / "SECURITY.md").read_text(encoding="utf-8").split("\n\n")[1]

    assert "Linux and macOS" in " ".join(first.split())
    assert "experimental" in first


def test_install_ties_the_status_line_to_the_claude_pool() -> None:
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    step = text[text.index("## 10. Claude Code status line") :]
    step = step[: step.index("\n## ", 1)]

    assert "`[statusline] pool`" in step
    assert "claude-pro" in step


CALLER_BACKENDS = {
    "claude-code": "claude",
    "kimi-code": "kimi",
    "opencode": "opencode",
    "cline": "cline",
}


@pytest.mark.parametrize("path", [TEMPLATE, *EXAMPLES], ids=lambda path: path.name)
def test_callers_exclude_the_pool_their_own_routes_spend(path: Path) -> None:
    config = load_config(path)

    for caller, backend in CALLER_BACKENDS.items():
        pools = {route.pool for route in config.routes if route.backend == backend}
        pool = config.callers.get(caller)
        if pool is not None and pools:
            assert pool in pools, (path.name, caller)


def test_cline_shares_the_opencode_go_pool_where_it_is_configured() -> None:
    for path in [TEMPLATE, *EXAMPLES]:
        config = load_config(path)
        if len(config.pools) == 1:
            continue  # one pool: callers are not mapped at all
        if "cline" in config.callers and any(pool.id == "opencode-go" for pool in config.pools):
            assert config.callers["cline"] == "opencode-go", path.name


def test_install_first_job_approves_before_waiting() -> None:
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    step = text[text.index("## 8. First job") :]
    step = step[: step.index("\n## ", 1)]

    assert "agent-router run --no-wait" in step
    assert step.index("agent-router approve") < step.index("agent-router wait")


def test_dashboard_login_names_the_admin_token_file() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")

    assert "contents of the\n`admin.token`" in readme
    assert "paste the contents of `admin.token`" in install


def test_install_intro_gives_pypi_users_the_path_step() -> None:
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    intro = text[: text.index("\n## ")]

    assert "uv tool install open-agent-router" in intro
    assert "uv tool update-shell" in intro


SKILLS = sorted((ROOT / "integrations").glob("*/agent-router-delegate/SKILL.md"))


def skill_callers() -> set[str]:
    names = set()
    for path in SKILLS:
        names |= set(re.findall(r"--caller ([a-z-]+)", path.read_text(encoding="utf-8")))
    return names


def test_skill_callers_are_the_five_harnesses() -> None:
    assert skill_callers() == {"claude-code", "kimi-code", "opencode", "cline", "codex"}


@pytest.mark.parametrize("path", [TEMPLATE, *EXAMPLES], ids=lambda path: path.name)
def test_every_example_maps_all_skill_callers(path: Path) -> None:
    assert skill_callers() <= set(load_config(path).callers), path.name


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_single_pool_examples_do_not_map_callers(path: Path) -> None:
    config = load_config(path)
    if len(config.pools) > 1:
        pytest.skip("several pools")

    assert set(config.callers.values()) == {None}, path.name
    callers = path.read_text(encoding="utf-8").split("[callers]", 1)[0].rsplit("\n\n", 1)[-1]
    section = path.read_text(encoding="utf-8").split("[callers]", 1)[1].split("\n[", 1)[0]
    assert "one pool" in (callers + section).lower(), path.name
