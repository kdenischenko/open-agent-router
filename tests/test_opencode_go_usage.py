"""OpenCode Go usage source: reads, failure reasons, registration and key secrecy."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_router import registry
from agent_router.collectors.opencode_go_usage import SOURCE, USAGE_URL, OpenCodeGoUsage
from agent_router.config import load_config
from agent_router.config_model import Pool, PoolUsage, Window
from agent_router.periods import Duration
from agent_router.usage import UsagePoint, observations

FIXTURES = Path(__file__).parent / "fixtures" / "opencode_go_usage"
KEY = "fake-go-key-for-tests"
NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)

CONFIG = """\
[[pools]]
id = "go"
title = "OpenCode Go"
unit = "percent"

[[pools.windows]]
id = "5h"
duration = "5h"
kind = "rolling"
role = "guard"

[[pools.windows]]
id = "week"
duration = "7d"
kind = "fixed"
role = "guard"

[[pools.windows]]
id = "month"
duration = "1mo"
kind = "fixed"
role = "pace"

[pools.usage]
source = "opencode-go"
api_key_env = "OPENCODE_GO_API_KEY"
windows = { rolling = "5h", weekly = "week", monthly = "month" }

[[routes]]
id = "route"
pool = "go"
tiers = ["standard"]
quality = 3
"""


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def responding(payload: bytes, status: int = 200, seen: list[httpx.Request] | None = None):
    """A MockTransport handler that answers every request the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=payload, headers={"Content-Type": "application/json"})

    return handler


def raising(exc: Exception):
    """A MockTransport handler that fails every request with the given error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def build(handler, env: dict[str, str] | None = None) -> OpenCodeGoUsage:
    """A source whose every request goes through the handler; no network is involved."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenCodeGoUsage({"OPENCODE_GO_API_KEY": KEY} if env is None else env, client=client)


def go_pool() -> Pool:
    """A pool shaped like the template's OpenCode Go pool, with all three meters mapped."""
    guard = Window("5h", Duration(hours=5), "rolling", "guard", None, None, 1.0)
    week = Window("week", Duration(days=7), "rolling", "guard", None, None, 1.0)
    month = Window("month", Duration(months=1), "fixed", "pace", None, None, 1.0)
    usage = PoolUsage(
        source="opencode-go",
        windows={"rolling": "5h", "weekly": "week", "monthly": "month"},
    )
    return Pool(
        "go",
        "OpenCode Go",
        "percent",
        (guard, week, month),
        0.0,
        None,
        None,
        None,
        None,
        usage=usage,
    )


def test_ok_read_reports_every_meter() -> None:
    seen: list[httpx.Request] = []
    read = build(responding(fixture("ok.json"), seen=seen)).read()

    assert read.ok
    assert read.points == (
        UsagePoint("rolling", 0.3, datetime(2026, 11, 21, 15, tzinfo=UTC)),
        UsagePoint("weekly", 1.0, datetime(2026, 11, 23, tzinfo=UTC)),
        UsagePoint("monthly", 0.5, datetime(2026, 12, 1, tzinfo=UTC)),
    )
    assert str(seen[0].url) == USAGE_URL
    assert seen[0].method == "GET"
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


def test_observations_carry_the_source_name() -> None:
    read = build(responding(fixture("ok.json"))).read()

    result = observations(go_pool(), SOURCE, read, NOW)

    assert [(item.window, item.fraction, item.source) for item in result] == [
        ("5h", 0.3, "opencode_go_usage"),
        ("week", 1.0, "opencode_go_usage"),
        ("month", 0.5, "opencode_go_usage"),
    ]
    # the fixed window keeps the vendor reset time; the rolling ones have none of their own
    assert [item.resets_at for item in result] == [None, None, datetime(2026, 12, 1, tzinfo=UTC)]


def test_missing_meter_is_skipped() -> None:
    payload = json.dumps(
        {
            "usage": {
                "monthly": {
                    "status": "ok",
                    "percent": 50,
                    "resetsAt": "2026-12-01T00:00:00.000Z",
                }
            }
        }
    ).encode()

    read = build(responding(payload)).read()

    assert read.points == (UsagePoint("monthly", 0.5, datetime(2026, 12, 1, tzinfo=UTC)),)


@pytest.mark.parametrize(
    "meter",
    [
        {"percent": "lots"},
        {"percent": 150},
        {"percent": -5},
        {"percent": None},
        {"percent": True},
        {"percent": 50, "resetsAt": "soon"},
        {"percent": 50, "resetsAt": "2026-12-01T00:00:00"},  # naive: no timezone
        "broken",
    ],
)
def test_bad_meter_is_skipped_others_kept(meter: object) -> None:
    body = json.loads(fixture("ok.json"))
    body["usage"]["weekly"] = meter

    read = build(responding(json.dumps(body).encode())).read()

    assert read.ok
    assert [point.field for point in read.points] == ["rolling", "monthly"]


@pytest.mark.parametrize(
    ("status", "payload", "reason"),
    [
        (401, b"{}", "unauthorized"),
        (403, fixture("forbidden.json"), "forbidden"),
        (301, b"{}", "http_error"),
        (418, b"{}", "http_error"),
        (500, b"{}", "http_error"),
    ],
)
def test_http_failures_report_reason_codes(status: int, payload: bytes, reason: str) -> None:
    read = build(responding(payload, status=status)).read()

    assert read.reason == reason
    assert read.points == ()


def test_timeout_reports_reason() -> None:
    assert build(raising(httpx.ReadTimeout("too slow"))).read().reason == "timeout"


def test_transport_error_reports_unavailable() -> None:
    assert build(raising(httpx.ConnectError("connection closed"))).read().reason == "unavailable"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"", b"{}", b"[]", b'{"usage": []}', b'{"usage": {}}', fixture("malformed.json")],
)
def test_malformed_body_reports_reason(payload: bytes) -> None:
    assert build(responding(payload)).read().reason == "malformed_response"


@pytest.mark.parametrize("env", [{}, {"OPENCODE_GO_API_KEY": ""}])
def test_missing_or_empty_key_needs_no_request(env: dict[str, str]) -> None:
    seen: list[httpx.Request] = []

    read = build(responding(fixture("ok.json"), seen=seen), env=env).read()

    assert read.reason == "missing_api_key"
    assert seen == []


def test_key_comes_from_the_configured_variable() -> None:
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(responding(fixture("ok.json"), seen=seen)))

    read = OpenCodeGoUsage({"MY_GO_KEY": KEY}, "MY_GO_KEY", client=client).read()

    assert read.ok
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


def test_factory_takes_the_key_variable_from_pool_usage() -> None:
    usage = PoolUsage(source="opencode-go", api_key_env="MY_GO_KEY", windows={"monthly": "month"})

    built = SOURCE.factory(usage, {})

    assert isinstance(built, OpenCodeGoUsage)
    # without a key no request is made, so the reads below stay offline
    assert built.read().reason == "missing_api_key"
    # a key under another variable name is not used
    assert SOURCE.factory(usage, {"OPENCODE_GO_API_KEY": KEY}).read().reason == "missing_api_key"


def test_registry_loads_the_registered_source() -> None:
    assert registry.load_usage_source("opencode-go") is SOURCE
    assert SOURCE.name == "opencode-go"
    assert SOURCE.windows == frozenset({"rolling", "weekly", "monthly"})
    assert SOURCE.observation_source == "opencode_go_usage"


def test_config_with_the_source_validates(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")

    config = load_config(path)

    usage = config.pools[0].usage
    assert usage.source == "opencode-go"
    assert usage.api_key_env == "OPENCODE_GO_API_KEY"
    assert dict(usage.windows) == {"rolling": "5h", "weekly": "week", "monthly": "month"}
    assert registry.validate(config) == []


def test_key_never_leaks(caplog: pytest.LogCaptureFixture) -> None:
    key = "distinctive-fake-key-leak-watch"
    env = {"OPENCODE_GO_API_KEY": key}
    cases = [
        build(responding(b"{}", status=401), env=env),
        build(responding(fixture("forbidden.json"), status=403), env=env),
        build(responding(b"{}", status=500), env=env),
        build(responding(b"not json"), env=env),
        build(raising(httpx.ReadTimeout("too slow")), env=env),
        build(raising(httpx.ConnectError("connection closed")), env=env),
        OpenCodeGoUsage({}, client=None),  # missing key: answered before any request
    ]

    with caplog.at_level(logging.DEBUG):
        for item in cases:
            read = item.read()
            assert not read.ok
            assert key not in repr(read)
            assert key not in repr(item)
            assert key not in (read.reason or "")
    assert key not in caplog.text
