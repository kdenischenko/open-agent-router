"""Harness and counter registry: entry points, backend sections, third-party counters."""

from __future__ import annotations

import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_router import cli, plugins
from agent_router.api import create_app
from agent_router.collectors.opencode_stats import CounterSampler
from agent_router.config import BackendSection, ConfigError
from agent_router.registry import (
    COUNTER_GROUP,
    HARNESS_GROUP,
    CounterSpec,
    HarnessSpec,
    harnesses,
    load_harness,
    resolve_backend,
)
from agent_router.runner import ResolvedExecutable
from helpers import BASE_URL, bearer, full_config_text, write_config

STUB = Path(__file__).parent / "stubs" / "fake_harness.py"

ROUTE_X = """
[[routes]]
id = "x/model"
pool = "opencode-go"
backend = "x"
model = "m"
tiers = ["cheap"]
"""


def fake_resolve(name: str) -> ResolvedExecutable:
    path = Path(name)
    if path.is_absolute():
        return ResolvedExecutable(path, False)
    return ResolvedExecutable(Path("/opt/harness") / name, False)


def route(route_id: str, backend: str) -> str:
    return ROUTE_X.replace('"x/model"', f'"{route_id}"').replace('"x"', f'"{backend}"')


def test_builtin_harnesses_registered() -> None:
    names = {entry.name for entry in metadata.entry_points(group=HARNESS_GROUP)}
    assert names >= {"kimi", "opencode", "claude", "cline"}
    assert load_harness("opencode").spend == "counter"
    assert load_harness("kimi").spend == "journal"
    assert load_harness("claude").spend == "journal"
    assert load_harness("opencode").executables == ("opencode",)
    assert load_harness("kimi").adapter.name == "kimi"


def test_unknown_harness_is_config_error(home: Path) -> None:
    text = full_config_text() + ROUTE_X + '\n[backends.x]\nharness = "nope"\n'
    with pytest.raises(ConfigError) as caught:
        write_config(home, text)
    message = str(caught.value)
    assert '"nope"' in message
    assert "available: claude, cline, kimi, opencode" in message


def test_unknown_counter_is_config_error(home: Path) -> None:
    text = full_config_text().replace('counter = "opencode_stats"', 'counter = "nope"')
    with pytest.raises(ConfigError) as caught:
        write_config(home, text)
    message = str(caught.value)
    assert '"nope"' in message
    assert "opencode_stats" in message


def test_two_sections_one_harness(home: Path) -> None:
    text = (
        full_config_text()
        + route("oc-a/m", "oc-a")
        + route("oc-b/m", "oc-b")
        + '\n[backends.oc-a]\nharness = "opencode"\n'
        + f"\n[backends.oc-b]\nharness = \"opencode\"\nexecutable = '{STUB}'\n"
    )
    config = write_config(home, text)
    specs = harnesses(config)
    assert {"oc-a", "oc-b"} <= set(specs)
    assert specs["oc-a"].adapter is specs["oc-b"].adapter
    path_a = resolve_backend(config.backend_section("oc-a"), specs["oc-a"], fake_resolve).path
    path_b = resolve_backend(config.backend_section("oc-b"), specs["oc-b"], fake_resolve).path
    assert path_a == Path("/opt/harness/opencode")
    assert path_b == STUB
    assert config.backend_section("oc-b") == BackendSection("oc-b", "opencode", str(STUB))


def test_resolve_backend_takes_first_found_executable() -> None:
    spec = HarnessSpec("two", load_harness("kimi").adapter, ("first", "second"), "journal")
    section = BackendSection("two", "two", "")
    seen: list[str] = []

    def resolve(name: str) -> ResolvedExecutable:
        seen.append(name)
        if name == "second":
            return ResolvedExecutable(Path("/opt/second"), False)
        return ResolvedExecutable(None, False)

    assert resolve_backend(section, spec, resolve).path == Path("/opt/second")
    assert seen == ["first", "second"]


def test_section_without_table_is_implicit(home: Path) -> None:
    text = full_config_text().replace('[backends.kimi]\nexecutable = ""\n', "")
    config = write_config(home, text)
    assert "kimi" not in config.backends
    assert config.backend_section("kimi") == BackendSection("kimi", "kimi", "")
    assert "kimi" in harnesses(config)


def test_counter_executable_from_registry(home: Path, store, clock, bus) -> None:
    text = full_config_text().replace(
        '[backends.opencode]\nexecutable = ""', "[backends.opencode]\nexecutable = 'oc-custom'"
    )
    config = write_config(home, text)
    called: list[str] = []

    def run(executable: str):
        called.append(executable)
        return None

    sampler = CounterSampler(config, store, clock, bus, run=run, resolve=fake_resolve)
    sampler.sample_once()
    assert called == [str(Path("/opt/harness/oc-custom"))]


class FakeEntry:
    def __init__(self, name, value):
        self.name = name
        self._value = value

    def load(self):
        return self._value


class FakeCounter:
    calls: list[str] = []

    def __init__(self, config, store, clock, bus) -> None:
        self.config = config

    def sample_once(self) -> None:
        FakeCounter.calls.append("sampled")


def test_third_party_counter_is_called(
    monkeypatch: pytest.MonkeyPatch, home: Path, store, clock, tokens, bus
) -> None:
    real = metadata.entry_points
    spec = CounterSpec("fake-counter-t1", FakeCounter, frozenset())

    def entry_points(*, group):
        found = list(real(group=group))
        if group == COUNTER_GROUP:
            found.append(FakeEntry("fake-counter-t1", spec))
        return found

    monkeypatch.setattr(plugins.metadata, "entry_points", entry_points)
    FakeCounter.calls = []
    text = full_config_text().replace('counter = "opencode_stats"', 'counter = "fake-counter-t1"')
    config = write_config(home, text)
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    assert "fake-counter-t1" in app.state.counters
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/api/v1/observations",
            json={"pool": "opencode-go", "window": "month", "percent": 10, "resets_in": "10d"},
            headers=bearer(tokens.admin),
        )
    assert response.status_code < 300, response.text
    assert FakeCounter.calls == ["sampled"]


@pytest.mark.parametrize(
    "module",
    [
        "agent_router.config",
        "agent_router.registry",
        "agent_router.adapters.kimi",
        "agent_router.adapters.opencode",
        "agent_router.adapters.claude",
        "agent_router.collectors.opencode_stats",
    ],
)
def test_imports_are_acyclic(module: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr


class StubGateway:
    def get(self, path: str, **params) -> dict:
        return {"seq": 0, "subscribers": {}}


def test_doctor_implicit_third_party_section(
    monkeypatch: pytest.MonkeyPatch, home: Path, register_harness
) -> None:
    from agent_router.auth import ensure_tokens

    register_harness(
        "third", HarnessSpec("third", load_harness("kimi").adapter, ("third-cli",), "journal")
    )
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)
    write_config(home, full_config_text() + route("x/model", "third"))
    ensure_tokens(home)
    monkeypatch.setattr(cli, "make_client", lambda base_url, token: StubGateway())
    monkeypatch.setattr(
        cli, "resolve_executable", lambda name: ResolvedExecutable(None, False), raising=False
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "third | no | — | — | x/model" in result.stdout  # non-ascii on purpose
    assert "Warning: route x/model is enabled, but harness third not found" in result.stdout
