"""Usage reader: scheduled and forced reads, events, API endpoint, CLI command, dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_router import cli, cli_common, registry
from agent_router.api import create_app
from agent_router.ledger import window_state
from agent_router.service import Gateway
from agent_router.store import Observation
from agent_router.usage import UsagePoint, UsageRead, UsageSourceSpec
from agent_router.usage_reader import MIN_REFRESH_S, UnknownPoolError, UsageReader
from helpers import BASE_URL, bearer, write_config
from test_web_dashboard import csrf_from, login
from test_web_limits import class_block, element_block

NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)
RESET = datetime(2026, 11, 16, 0, tzinfo=UTC)  # within one week window of the clock

CONFIG_TEXT = """\
[usage]
mode = "auto"
interval = "5m"

[[pools]]
id = "auto-pool"
title = "Auto Pool"
unit = "percent"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "pace"

[[pools.windows]]
id = "5h"
duration = "5h"
kind = "rolling"
role = "guard"

[pools.usage]
source = "fake-usage"
api_key_env = "FAKE_USAGE_KEY"
windows = { monthly = "week", rolling = "5h" }

[[pools]]
id = "manual-pool"
title = "Manual Pool"
unit = "percent"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "pace"

[pools.usage]
source = "fake-usage"
mode = "manual"
windows = { monthly = "week" }

[[pools]]
id = "plain"
title = "Plain"
unit = "percent"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "pace"

[[routes]]
id = "route"
pool = "auto-pool"
tiers = ["standard"]
quality = 3
"""


class FakeSource:
    """Usage source stub: counts reads, returns a canned read or raises."""

    def __init__(self, result: UsageRead | None = None, error: Exception | None = None) -> None:
        self.result = (
            UsageRead(points=(UsagePoint("monthly", 0.4, RESET), UsagePoint("rolling", 0.25)))
            if result is None
            else result
        )
        self.error = error
        self.reads = 0

    def read(self) -> UsageRead:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.result


def install_fake_spec(monkeypatch: pytest.MonkeyPatch, factory: Any) -> UsageSourceSpec:
    spec = UsageSourceSpec(
        name="fake-usage",
        factory=factory,
        windows=frozenset({"monthly", "rolling"}),
        observation_source="fake_usage",
    )
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)
    return spec


@pytest.fixture(autouse=True)
def fake_spec(monkeypatch: pytest.MonkeyPatch) -> UsageSourceSpec:
    return install_fake_spec(monkeypatch, lambda pool_usage, env: FakeSource())


@pytest.fixture
def usage_config(home: Path):
    return write_config(home, CONFIG_TEXT)


@pytest.fixture
def events(bus):
    seen: list = []
    bus.subscribe("usage-probe", seen.append)

    def collect(only_usage: bool = True):
        bus.close()  # flush the remainder; no publications reach the subscriber after close
        found = [(event.type, dict(event.data)) for event in seen]
        return [item for item in found if item[0].startswith("usage.")] if only_usage else found

    return collect


def make_reader(usage_config, store, clock, tokens, home, bus, sources=None) -> UsageReader:
    gateway = Gateway(usage_config, store, clock, tokens, home=home, tz=UTC, bus=bus)
    return UsageReader(usage_config, gateway, clock, bus, environ={}, sources=sources)


# --- reader: schedule, rate limit, manual mode ---------------------------------------


def test_scheduled_refresh_reads_auto_pool_once_per_interval(
    usage_config, store, clock, tokens, home, bus
) -> None:
    fake = FakeSource()
    reader = make_reader(
        usage_config, store, clock, tokens, home, bus, {"auto-pool": fake, "manual-pool": fake}
    )

    result = reader.refresh(scheduled=True)
    assert [item.pool for item in result.read] == ["auto-pool"]
    assert fake.reads == 1

    clock.advance(timedelta(minutes=1))  # the background thread ticks faster than the interval
    result = reader.refresh(scheduled=True)
    assert result.read == ()
    assert [(item.pool, item.reason) for item in result.skipped] == [
        ("auto-pool", "too_soon"),
        ("manual-pool", "manual"),
    ]
    assert fake.reads == 1

    clock.advance(timedelta(minutes=4))  # 5 min in total: the interval has passed
    result = reader.refresh(scheduled=True)
    assert [item.pool for item in result.read] == ["auto-pool"]
    assert fake.reads == 2


def test_manual_pool_is_never_read(usage_config, store, clock, tokens, home, bus) -> None:
    fake = FakeSource()
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"manual-pool": fake})

    forced = reader.refresh("manual-pool")
    scheduled = reader.refresh("manual-pool", scheduled=True)

    assert [(item.pool, item.reason) for item in forced.skipped] == [("manual-pool", "manual")]
    assert [(item.pool, item.reason) for item in scheduled.skipped] == [("manual-pool", "manual")]
    assert forced.read == () and scheduled.read == ()
    assert fake.reads == 0


def test_forced_refresh_within_a_minute_is_too_soon(
    usage_config, store, clock, tokens, home, bus
) -> None:
    fake = FakeSource()
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})

    assert [item.pool for item in reader.refresh().read] == ["auto-pool"]

    clock.advance(timedelta(seconds=MIN_REFRESH_S - 1))
    skipped = {item.pool: item.reason for item in reader.refresh().skipped}
    assert skipped["auto-pool"] == "too_soon"
    assert fake.reads == 1

    clock.advance(timedelta(seconds=1))
    assert [item.pool for item in reader.refresh().read] == ["auto-pool"]
    assert fake.reads == 2


def test_pool_without_source_is_skipped_and_state_is_none(
    usage_config, store, clock, tokens, home, bus
) -> None:
    reader = make_reader(usage_config, store, clock, tokens, home, bus)

    result = reader.refresh("plain")

    assert [(item.pool, item.reason) for item in result.skipped] == [("plain", "no_source")]
    assert reader.state("plain") is None
    assert reader.state("auto-pool") is not None  # the source was built by the factory


def test_unknown_pool_raises(usage_config, store, clock, tokens, home, bus) -> None:
    reader = make_reader(usage_config, store, clock, tokens, home, bus)

    with pytest.raises(UnknownPoolError, match="unknown pool: nope"):
        reader.refresh("nope")


# --- reader: failures, events, observations -------------------------------------------


def test_failed_read_publishes_event_and_writes_nothing(
    usage_config, store, clock, tokens, home, bus, events
) -> None:
    fake = FakeSource(result=UsageRead(reason="unauthorized"))
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})

    result = reader.refresh("auto-pool")

    assert result.read == ()
    assert [(item.pool, item.reason) for item in result.failed] == [("auto-pool", "unauthorized")]
    assert store.last_observation("auto-pool", "week") is None
    assert events() == [
        (
            "usage.read_failed",
            {"pool": "auto-pool", "source": "fake-usage", "reason": "unauthorized"},
        )
    ]
    state = reader.state("auto-pool")
    assert state is not None
    assert state.last_attempt_at == clock.now()
    assert state.last_success_at is None
    assert state.last_reason == "unauthorized"


def test_source_exception_becomes_reason_error(
    usage_config, store, clock, tokens, home, bus, events
) -> None:
    fake = FakeSource(error=RuntimeError("boom"))
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})

    result = reader.refresh("auto-pool")

    assert [(item.pool, item.reason) for item in result.failed] == [("auto-pool", "error")]
    found = events()
    assert found == [
        ("usage.read_failed", {"pool": "auto-pool", "source": "fake-usage", "reason": "error"})
    ]
    assert "boom" not in str(found)  # the exception text never reaches the bus


def test_successful_read_writes_observations_and_newest_wins(
    usage_config, store, clock, tokens, home, bus, events
) -> None:
    fake = FakeSource()
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})
    pool = usage_config.pools[0]
    week = next(window for window in pool.windows if window.id == "week")
    # an older manual calibration must lose to the newer source observation
    store.add_observation(
        Observation("auto-pool", "week", 0.9, clock.now() - timedelta(hours=1), None, "manual")
    )

    result = reader.refresh("auto-pool")

    assert [(item.pool, item.windows) for item in result.read] == [("auto-pool", ("week", "5h"))]
    assert events() == [
        ("usage.read", {"pool": "auto-pool", "source": "fake-usage", "windows": ["week", "5h"]})
    ]
    last = store.last_observation("auto-pool", "week")
    assert last is not None
    assert last.source == "fake_usage"  # the spec's observation_source, not the spec name
    assert last.fraction == 0.4
    rolling = store.last_observation("auto-pool", "5h")
    assert rolling is not None
    assert rolling.resets_at is None  # a rolling window never carries a reset
    state = window_state(pool, week, store, clock.now())
    assert state.used == pytest.approx(0.4)
    assert state.source == "fake_usage"
    read_state = reader.state("auto-pool")
    assert read_state is not None
    assert read_state.last_success_at == clock.now()
    assert read_state.last_reason is None


# --- API: POST /api/v1/usage/refresh ---------------------------------------------------


@pytest.fixture
def api_client(tmp_path, store, clock, tokens, bus, monkeypatch):
    fake = FakeSource()
    install_fake_spec(monkeypatch, lambda pool_usage, env: fake)
    home = tmp_path / "app-home"
    config = write_config(home, CONFIG_TEXT)
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client, app, fake


def test_api_refresh_requires_admin(api_client, tokens) -> None:
    client, _, _ = api_client

    assert client.post("/api/v1/usage/refresh").status_code == 401
    response = client.post("/api/v1/usage/refresh", headers=bearer(tokens.client))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_api_refresh_unknown_pool_is_422(api_client, tokens) -> None:
    client, _, _ = api_client

    response = client.post(
        "/api/v1/usage/refresh", json={"pool": "nope"}, headers=bearer(tokens.admin)
    )

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "unknown_pool", "message": "unknown pool: nope"}}


def test_api_refresh_result_shape_and_rate_limit(api_client, tokens, clock) -> None:
    client, app, fake = api_client
    assert app.state.usage_reader is not None
    assert app.state.gateway.usage_reader is app.state.usage_reader

    response = client.post("/api/v1/usage/refresh", json={}, headers=bearer(tokens.admin))

    assert response.status_code == 200
    assert response.json() == {
        "read": [{"pool": "auto-pool", "windows": ["week", "5h"]}],
        "failed": [],
        "skipped": [{"pool": "manual-pool", "reason": "manual"}],
    }
    assert fake.reads == 1

    second = client.post("/api/v1/usage/refresh", headers=bearer(tokens.admin))
    assert second.json()["read"] == []
    assert {"pool": "auto-pool", "reason": "too_soon"} in second.json()["skipped"]
    assert fake.reads == 1

    clock.advance(timedelta(seconds=MIN_REFRESH_S))
    third = client.post(
        "/api/v1/usage/refresh", json={"pool": "auto-pool"}, headers=bearer(tokens.admin)
    )
    assert [item["pool"] for item in third.json()["read"]] == ["auto-pool"]
    assert fake.reads == 2


# --- CLI: agent-router usage refresh ----------------------------------------------------


class RecordingGateway:
    """Gateway client stub: answers post with a canned refresh result."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, dict | None]] = []

    def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append((path, body))
        return self.result


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_home(home: Path, config, tokens, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)


def cli_gateway_stub(monkeypatch: pytest.MonkeyPatch, result: dict, seen: list[str]) -> Any:
    gateway = RecordingGateway(result)

    def factory(base_url: str, token: str):
        seen.append(token)
        return gateway

    monkeypatch.setattr(cli_common, "make_client", factory)
    return gateway


def test_cli_usage_refresh_prints_one_line_per_pool(
    runner, cli_home, tokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    gateway = cli_gateway_stub(
        monkeypatch,
        {
            "read": [{"pool": "a", "windows": ["week", "5h"]}],
            "failed": [{"pool": "b", "reason": "unauthorized"}],
            "skipped": [{"pool": "c", "reason": "too_soon"}],
        },
        seen,
    )

    result = runner.invoke(cli.app, ["usage", "refresh"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert "Read: a (week, 5h)" in lines
    assert "Failed: b — unauthorized" in lines
    assert "Skipped: c — too_soon" in lines
    assert gateway.calls == [("/api/v1/usage/refresh", {})]
    assert seen == [tokens.admin]


def test_cli_usage_refresh_sends_the_pool(
    runner, cli_home, tokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    gateway = cli_gateway_stub(monkeypatch, {"read": [], "failed": [], "skipped": []}, seen)

    result = runner.invoke(cli.app, ["usage", "refresh", "auto-pool"])

    assert result.exit_code == 0
    assert result.output == ""
    assert gateway.calls == [("/api/v1/usage/refresh", {"pool": "auto-pool"})]


# --- dashboard: buttons and POST /limits/refresh ----------------------------------------


@pytest.fixture
def web_client(tmp_path, store, clock, tokens, bus, monkeypatch):
    fake = FakeSource()
    install_fake_spec(monkeypatch, lambda pool_usage, env: fake)
    home = tmp_path / "web-home"
    config = write_config(home, CONFIG_TEXT)
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        assert login(client, tokens.admin).status_code == 303
        yield client, fake


@pytest.mark.parametrize("path", ["/", "/fragments/limits"])
def test_refresh_button_for_auto_pool_only(web_client, path) -> None:
    client, _ = web_client
    response = client.get(path)
    text = response.text

    card = element_block(text, r'<article\b[^>]*id="limit-auto-pool"[^>]*>')
    form = class_block(card, "limit-refresh")
    opener = form[: form.index(">") + 1]
    assert 'method="post"' in opener
    assert 'action="/limits/refresh"' in opener
    assert f'name="csrf" value="{csrf_from(response)}"' in form
    assert 'name="pool" value="auto-pool"' in form
    assert "Refresh limits" in form

    manual = element_block(text, r'<article\b[^>]*id="limit-manual-pool"[^>]*>')
    assert "limit-refresh" not in manual
    assert "limit-refresh" not in element_block(text, r'<article\b[^>]*id="limit-plain"[^>]*>')


def test_global_refresh_button_on_the_limits_page(web_client) -> None:
    client, _ = web_client

    for path in ("/", "/fragments/limits"):
        text = client.get(path).text
        form = class_block(text, "limits-refresh")
        assert 'action="/limits/refresh"' in form
        assert 'name="pool"' not in form


def test_post_limits_refresh_without_valid_csrf_is_rejected(web_client) -> None:
    client, fake = web_client

    response = client.post("/limits/refresh", data={"csrf": "bogus"}, follow_redirects=False)

    assert response.status_code == 403
    assert "invalid CSRF token" in response.text
    assert fake.reads == 0


def test_post_limits_refresh_reads_pool_and_redirects(web_client) -> None:
    client, fake = web_client
    csrf = csrf_from(client.get("/"))

    response = client.post(
        "/limits/refresh", data={"csrf": csrf, "pool": "auto-pool"}, follow_redirects=False
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/")
    assert location.endswith("#limit-auto-pool")
    assert fake.reads == 1
    page = client.get(location)
    assert "Read: auto-pool (week, 5h)" in page.text


def test_post_limits_refresh_without_pool_reads_all(web_client, clock) -> None:
    client, fake = web_client
    csrf = csrf_from(client.get("/"))

    response = client.post("/limits/refresh", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("#limits")
    assert fake.reads == 1  # the manual pool is skipped without a read
    page = client.get(response.headers["location"])
    assert "Read: auto-pool (week, 5h)" in page.text
    assert "Skipped: manual-pool — manual" in page.text


def test_post_limits_refresh_without_session_redirects_to_login(
    tmp_path, store, clock, tokens, bus, monkeypatch
) -> None:
    install_fake_spec(monkeypatch, lambda pool_usage, env: FakeSource())
    home = tmp_path / "anon-home"
    config = write_config(home, CONFIG_TEXT)
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post("/limits/refresh", data={}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- reader: concurrency, empty reads, calibration hooks --------------------------------


def test_refresh_during_a_read_of_the_same_pool_is_too_soon(
    usage_config, store, clock, tokens, home, bus
) -> None:
    # a second refresh arriving while the first one is still reading (the background thread
    # and a request at the same moment) must not read the source again
    nested: list = []

    class Reentrant(FakeSource):
        def read(self) -> UsageRead:
            if not nested:
                nested.append(reader.refresh("auto-pool"))
            return super().read()

    fake = Reentrant()
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})

    outer = reader.refresh("auto-pool")

    assert [item.pool for item in outer.read] == ["auto-pool"]
    assert [(item.pool, item.reason) for item in nested[0].skipped] == [("auto-pool", "too_soon")]
    assert fake.reads == 1


def test_ok_read_without_usable_points_is_a_failure(
    usage_config, store, clock, tokens, home, bus, events
) -> None:
    fake = FakeSource(UsageRead(points=(UsagePoint("unmapped", 0.4),)))
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": fake})

    result = reader.refresh("auto-pool")

    assert [(item.pool, item.reason) for item in result.failed] == [("auto-pool", "no_data")]
    assert events() == [
        ("usage.read_failed", {"pool": "auto-pool", "source": "fake-usage", "reason": "no_data"})
    ]
    state = reader.state("auto-pool")
    assert state is not None
    assert state.last_success_at is None
    assert state.last_reason == "no_data"


def test_successful_read_runs_the_calibration_hooks(
    usage_config, store, clock, tokens, home, bus
) -> None:
    reader = make_reader(usage_config, store, clock, tokens, home, bus, {"auto-pool": FakeSource()})
    called: list[str] = []
    reader._gateway.after_calibration.append(called.append)

    reader.refresh("auto-pool")

    assert called == ["auto-pool"]
