from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_router.api import create_app
from agent_router.collectors.claude_statusline import (
    accept_statusline,
    observations_from_rate_limits,
)
from agent_router.config import StatuslineSettings
from agent_router.store import Observation
from helpers import BASE_URL, bearer, full_config_text, read_only_route, write_config

SAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_statusline.json").read_text(encoding="utf-8")
)
FIVE_RESET = datetime.fromtimestamp(SAMPLE["rate_limits"]["five_hour"]["resets_at"], UTC)
WEEK_RESET = datetime.fromtimestamp(SAMPLE["rate_limits"]["seven_day"]["resets_at"], UTC)
SETTINGS = StatuslineSettings(pool="claude-pro")
OBSERVATIONS = "/api/v1/observations"
SIGNAL = "/api/v1/signals/claude-statusline"


@pytest.fixture(autouse=True)
def before_sample_resets(clock):
    clock.set(FIVE_RESET - timedelta(hours=2))


def window_of(client, tokens, pool, window):
    body = client.get("/api/v1/snapshot", headers=bearer(tokens.client)).json()
    found = next(item for item in body["pools"] if item["id"] == pool)
    return next(item for item in found["windows"] if item["id"] == window)


def calibrate(client, tokens, **body):
    return client.post(OBSERVATIONS, json=body, headers=bearer(tokens.admin))


def signal(client, tokens, rate_limits):
    response = client.post(SIGNAL, json={"rate_limits": rate_limits}, headers=bearer(tokens.client))
    assert response.status_code == 200
    return response.json()["accepted"]


def limits(five=None, week=None, five_reset=FIVE_RESET, week_reset=WEEK_RESET):
    block = {}
    if five is not None:
        block["five_hour"] = {"used_percentage": five, "resets_at": five_reset.timestamp()}
    if week is not None:
        block["seven_day"] = {"used_percentage": week, "resets_at": week_reset.timestamp()}
    return block


@pytest.fixture
def events(bus):
    seen = []
    bus.subscribe("probe", seen.append)

    def collect():
        bus.close()  # flush the remainder; the subscriber gets no publications after close
        return [(event.type, dict(event.data)) for event in seen]

    return collect


# --- parsing the rate_limits block ------------------------------------------------


def test_sample_gives_two_observations(clock):
    found = observations_from_rate_limits(SAMPLE["rate_limits"], clock.now(), SETTINGS)
    assert [(o.pool, o.window, o.fraction, o.resets_at, o.source) for o in found] == [
        ("claude-pro", "5h", 0.42, FIVE_RESET, "claude_statusline"),
        ("claude-pro", "week", 0.615, WEEK_RESET, "claude_statusline"),
    ]
    assert all(o.observed_at == clock.now() for o in found)


def test_missing_or_broken_window_is_skipped(clock):
    now = clock.now()
    only_five = observations_from_rate_limits(limits(five=10), now, SETTINGS)
    assert [o.window for o in only_five] == ["5h"]
    block = limits(five=10, week=20)
    block["five_hour"]["used_percentage"] = "42"
    assert [o.window for o in observations_from_rate_limits(block, now, SETTINGS)] == ["week"]
    block = limits(five=10, week=20)
    block["seven_day"] = None
    assert [o.window for o in observations_from_rate_limits(block, now, SETTINGS)] == ["5h"]
    block = limits(five=10)
    block["five_hour"]["resets_at"] = None
    assert observations_from_rate_limits(block, now, SETTINGS) == []
    over = observations_from_rate_limits(limits(five=140, week=-1), now, SETTINGS)
    assert [(o.window, o.fraction) for o in over] == [("5h", 1.0)]
    custom = StatuslineSettings(pool="p", five_hour="short", seven_day="long")
    assert [
        (o.pool, o.window) for o in observations_from_rate_limits(limits(1, 2), now, custom)
    ] == [
        ("p", "short"),
        ("p", "long"),
    ]


@pytest.mark.parametrize("block", [None, "junk", 5, [], {}, {"five_hour": "x"}, {"five_hour": []}])
def test_garbage_block_gives_nothing(block, clock):
    assert observations_from_rate_limits(block, clock.now(), SETTINGS) == []


def test_accept_statusline_rule(clock):
    now = clock.now()

    def obs(fraction, resets=FIVE_RESET, source="claude_statusline"):
        return Observation("claude-pro", "5h", fraction, now, resets, source)

    last = obs(0.55)
    assert accept_statusline(obs(0.55), None, now) is True
    assert accept_statusline(obs(0.40), last, now) is False  # same window, lower usage
    assert accept_statusline(obs(0.40, FIVE_RESET + timedelta(minutes=9)), last, now) is False
    assert accept_statusline(obs(0.55), last, now) is True
    assert accept_statusline(obs(0.60), last, now) is True
    assert accept_statusline(obs(0.05, FIVE_RESET + timedelta(hours=5)), last, now) is True  # new
    assert accept_statusline(obs(0.90, FIVE_RESET - timedelta(minutes=11)), last, now) is False
    assert accept_statusline(obs(0.90, FIVE_RESET - timedelta(minutes=9)), last, now) is True
    assert accept_statusline(obs(0.90, now), last, now) is False  # reset already in the past
    assert accept_statusline(obs(0.90, now - timedelta(hours=1)), None, now) is False
    assert accept_statusline(obs(0.10), obs(0.55, None, "manual"), now) is True


# --- manual calibration -----------------------------------------------------------


def test_calibration_changes_snapshot(client, tokens, clock):
    reset = clock.now() + timedelta(days=2)
    response = calibrate(
        client, tokens, pool="claude-pro", window="week", percent=37.5, resets_at=reset.isoformat()
    )
    assert response.status_code == 200
    assert response.json()["added"] is True and response.json()["source"] == "manual"
    window = window_of(client, tokens, "claude-pro", "week")
    assert window["known"] is True and window["used"] == 0.375
    assert window["source"] == "manual" and window["resets_at"] == reset.isoformat()


def test_calibration_reports_the_kept_window_reset(client, tokens, clock):
    reset = clock.now() + timedelta(days=2)
    calibrate(
        client, tokens, pool="claude-pro", window="week", percent=10, resets_at=reset.isoformat()
    )
    again = calibrate(client, tokens, pool="claude-pro", window="week", percent=20)
    assert again.status_code == 200
    assert again.json()["resets_at"] is None
    assert again.json()["window_resets_at"] == reset.isoformat()
    rolling = calibrate(client, tokens, pool="kimi-code", window="5h", percent=20)
    assert rolling.json()["window_resets_at"] is None


def test_calibration_with_resets_in(client, tokens, clock):
    response = calibrate(
        client, tokens, pool="kimi-code", window="month", percent=10, resets_in="3h20m"
    )
    assert response.status_code == 200
    expected = clock.now() + timedelta(hours=3, minutes=20)
    assert window_of(client, tokens, "kimi-code", "month")["resets_at"] == expected.isoformat()


def test_calibration_needs_admin(client, tokens):
    body = {"pool": "claude-pro", "window": "week", "percent": 10}
    assert client.post(OBSERVATIONS, json=body).status_code == 401
    forbidden = client.post(OBSERVATIONS, json=body, headers=bearer(tokens.client))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"
    assert window_of(client, tokens, "claude-pro", "week")["known"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"pool": "no-such-pool", "window": "week", "percent": 10},
        {"pool": "claude-pro", "window": "no-such-window", "percent": 10},
        {"pool": "claude-pro", "window": "week", "percent": 101},
        {"pool": "claude-pro", "window": "week", "percent": -1},
        {"pool": "claude-pro", "window": "week", "percent": "10"},
        {"pool": "claude-pro", "window": "week"},
        {
            "pool": "claude-pro",
            "window": "week",
            "percent": 10,
            "resets_at": "2099-01-01T00:00:00+00:00",
            "resets_in": "3h",
        },
        {"pool": "claude-pro", "window": "week", "percent": 10, "resets_at": "2099-01-01T00:00:00"},
        {"pool": "claude-pro", "window": "week", "percent": 10, "resets_at": "tomorrow"},
        {"pool": "claude-pro", "window": "week", "percent": 10, "resets_in": "soon"},
        {
            "pool": "claude-pro",
            "window": "week",
            "percent": 10,
            "resets_at": "2000-01-01T00:00:00Z",
        },
        {"pool": "claude-pro", "window": "week", "percent": 10, "extra": 1},
    ],
)
def test_bad_calibration_is_422(client, tokens, store, body):
    response = calibrate(client, tokens, **body)
    assert response.status_code == 422
    assert set(response.json()["error"]) == {"code", "message"}
    assert store.last_observation("claude-pro", "week") is None


def test_manual_calibration_downwards_is_accepted(client, tokens, store):
    assert calibrate(client, tokens, pool="claude-pro", window="5h", percent=55).status_code == 200
    assert calibrate(client, tokens, pool="claude-pro", window="5h", percent=40).status_code == 200
    assert store.last_observation("claude-pro", "5h").fraction == 0.40


# --- status line signal -----------------------------------------------------------


def test_sample_signal_creates_two_observations(client, tokens, store):
    response = client.post(SIGNAL, json=SAMPLE, headers=bearer(tokens.client))
    assert response.status_code == 200 and response.json() == {"accepted": 2}
    five = store.last_observation("claude-pro", "5h")
    week = store.last_observation("claude-pro", "week")
    assert (five.fraction, five.resets_at, five.source) == (0.42, FIVE_RESET, "claude_statusline")
    assert (week.fraction, week.resets_at, week.source) == (0.615, WEEK_RESET, "claude_statusline")
    assert window_of(client, tokens, "claude-pro", "week")["used"] == 0.615


def test_signal_needs_token_and_takes_admin_too(client, tokens):
    assert client.post(SIGNAL, json=SAMPLE).status_code == 401
    assert client.post(SIGNAL, json=SAMPLE, headers=bearer(tokens.admin)).json() == {"accepted": 2}


def test_only_five_hour_gives_one(client, tokens, store):
    assert signal(client, tokens, limits(five=12)) == 1
    assert store.last_observation("claude-pro", "week") is None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"null",
        b"[]",
        b'"rate_limits"',
        b"not json",
        b"\xff\xfe",
        b'{"rate_limits": null}',
        b'{"rate_limits": "x"}',
        b'{"rate_limits": {"five_hour": {"used_percentage": "42", "resets_at": "x"}}}',
        b'{"rate_limits": {"five_hour": null, "seven_day": 7}}',
        b'{"other": 1}',
    ],
)
def test_garbage_signal_is_200_and_zero(client, tokens, store, content):
    headers = bearer(tokens.client) | {"Content-Type": "application/json"}
    response = client.post(SIGNAL, content=content, headers=headers)
    assert response.status_code == 200 and response.json() == {"accepted": 0}
    assert store.last_observation("claude-pro", "5h") is None


def test_twenty_equal_signals_keep_one_row_per_window(client, tokens, store, clock):
    for _ in range(20):
        assert signal(client, tokens, SAMPLE["rate_limits"]) == 2
        clock.advance(timedelta(seconds=3))
    assert len(store.observations("claude-pro", "5h")) == 1
    assert len(store.observations("claude-pro", "week")) == 1
    last = store.last_observation("claude-pro", "5h")
    assert last.observed_at == clock.now() - timedelta(seconds=3)


def test_stale_signals_are_dropped(client, tokens, store, clock):
    assert signal(client, tokens, limits(five=55)) == 1
    assert signal(client, tokens, limits(five=40)) == 0  # same window, lower usage
    assert store.last_observation("claude-pro", "5h").fraction == 0.55
    old = FIVE_RESET - timedelta(minutes=11)
    assert signal(client, tokens, limits(five=90, five_reset=old)) == 0  # past window
    assert signal(client, tokens, limits(five=90, five_reset=clock.now())) == 0  # reset has passed
    assert signal(client, tokens, limits(five=60)) == 1  # same window, higher usage
    assert store.last_observation("claude-pro", "5h").fraction == 0.60
    assert len(store.observations("claude-pro", "5h")) == 2


def test_signal_after_manual_calibration_follows_the_rule(client, tokens, store, clock):
    reset = FIVE_RESET.isoformat()
    calibrate(client, tokens, pool="claude-pro", window="5h", percent=55, resets_at=reset)
    assert signal(client, tokens, limits(five=40)) == 0
    assert calibrate(client, tokens, pool="claude-pro", window="5h", percent=30).status_code == 200
    assert (
        signal(client, tokens, limits(five=40)) == 1
    )  # a manual observation without a reset time leaves the window unknown


def test_statusline_not_configured(store, clock, tokens, home):
    text = full_config_text().replace('[statusline]\npool = "claude-pro"\n', "")
    config = write_config(home, text)
    assert config.statusline is None
    app = create_app(config, store, clock, tokens, home=home, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        assert signal(client, tokens, SAMPLE["rate_limits"]) == 0
        assert store.last_observation("claude-pro", "5h") is None  # exit closes the database


# --- advice -----------------------------------------------------------------------


def test_advice_returns_chosen_and_excluded(client, tokens):
    calibrate(client, tokens, pool="kimi-code", window="month", percent=20, resets_in="10d")
    response = client.get("/api/v1/advice?tier=standard", headers=bearer(tokens.client))
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "standard"
    assert [item["route"] for item in body["chosen"]][0] == "kimi-cli/k3.1-pro"
    assert all(item["reason"] and item["pool"] for item in body["chosen"] + body["excluded"])
    excluded = {item["route"]: item["reason"] for item in body["excluded"]}
    assert excluded["claude-cli/sonnet"] == "route disabled"
    assert excluded["opencode-cli/glm-5.3-flash"] == "does not serve tier standard"


def test_advice_defaults_caller_and_tier_checks(client, tokens, config):
    headers = bearer(tokens.client)
    assert client.get("/api/v1/advice").status_code == 401
    assert client.get("/api/v1/advice", headers=headers).json()["tier"] == "standard"
    unknown = client.get("/api/v1/advice?tier=huge", headers=headers)
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "bad_request"
    caller = next(name for name, pool in config.callers.items() if pool == "kimi-code")
    body = client.get("/api/v1/advice", params={"tier": "cheap", "caller": caller}, headers=headers)
    reasons = {item["route"]: item["reason"] for item in body.json()["excluded"]}
    assert reasons["kimi-cli/kimi-for-coding"] == "caller pool"
    assert [item["route"] for item in body.json()["chosen"]] == ["opencode-cli/glm-5.3-flash"]


def test_advice_takes_a_job_mode(home, store, clock, tokens):
    config = write_config(home, read_only_route(full_config_text(), "kimi-cli/k3.1-pro"))
    app = create_app(config, store, clock, tokens, home=home, background=False)
    headers = bearer(tokens.client)
    with TestClient(app, base_url=BASE_URL) as client:
        write = client.get("/api/v1/advice", params={"mode": "write"}, headers=headers).json()
        unknown = client.get("/api/v1/advice", params={"mode": "edit"}, headers=headers)

    assert write["mode"] == "write"
    reasons = {item["route"]: item["reason"] for item in write["excluded"]}
    assert reasons["kimi-cli/k3.1-pro"] == "mode write not supported"
    assert unknown.status_code == 422
    assert unknown.json()["error"] == {"code": "bad_request", "message": "unknown mode: edit"}


# --- events -----------------------------------------------------------------------


def test_calibration_publishes_added_and_snapshot_is_fresh(client, tokens, events):
    assert window_of(client, tokens, "claude-pro", "5h")["known"] is False  # snapshot is cached
    assert calibrate(client, tokens, pool="claude-pro", window="5h", percent=25).status_code == 200
    assert window_of(client, tokens, "claude-pro", "5h")["used"] == 0.25  # the clock has not moved
    assert events() == [
        (
            "observation.added",
            {"pool": "claude-pro", "window": "5h", "source": "manual", "fraction": 0.25},
        )
    ]


def test_repeat_publishes_updated_and_snapshot_shows_new_time(client, tokens, clock, events):
    calibrate(client, tokens, pool="claude-pro", window="5h", percent=25)
    first = window_of(client, tokens, "claude-pro", "5h")["observed_at"]
    clock.advance(timedelta(seconds=0.5))  # snapshot lifetime (1 s) has not expired
    assert (
        calibrate(client, tokens, pool="claude-pro", window="5h", percent=25).json()["added"]
        is False
    )
    second = window_of(client, tokens, "claude-pro", "5h")["observed_at"]
    assert first != second and second == clock.now().isoformat()
    assert [kind for kind, _ in events()] == ["observation.added", "observation.updated"]


def test_signal_events_and_dropped_signal_is_silent(client, tokens, events):
    assert signal(client, tokens, limits(five=55)) == 1
    assert signal(client, tokens, limits(five=40)) == 0
    assert signal(client, tokens, None) == 0
    assert calibrate(client, tokens, pool="nope", window="5h", percent=1).status_code == 422
    assert events() == [
        (
            "observation.added",
            {"pool": "claude-pro", "window": "5h", "source": "claude_statusline", "fraction": 0.55},
        )
    ]


# --- calibration reset checks -----------------------------------------------------


def test_calibrate_rejects_reset_for_rolling_window(client, tokens, store, clock):
    soon = (clock.now() + timedelta(hours=2)).isoformat()
    for extra in ({"resets_in": "2h"}, {"resets_at": soon}):
        response = calibrate(client, tokens, pool="kimi-code", window="5h", percent=10, **extra)
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "bad_request",
            "message": "window 5h is rolling: it has no reset time, calibrate without a reset",
        }
    assert store.last_observation("kimi-code", "5h") is None
    assert calibrate(client, tokens, pool="kimi-code", window="5h", percent=10).status_code == 200


def test_calibrate_rejects_reset_beyond_one_window(client, tokens, store, clock):
    far = calibrate(client, tokens, pool="claude-pro", window="5h", percent=10, resets_in="5h10m")
    assert far.status_code == 422
    assert far.json()["error"]["message"] == "reset is more than one 5h window ahead"
    assert store.last_observation("claude-pro", "5h") is None
    near = calibrate(client, tokens, pool="claude-pro", window="5h", percent=10, resets_in="4h")
    assert near.status_code == 200
    edge = (clock.now() + timedelta(hours=5, minutes=5)).isoformat()
    assert (
        calibrate(
            client, tokens, pool="claude-pro", window="5h", percent=10, resets_at=edge
        ).status_code
        == 200
    )


def test_calibrate_month_window_edge(client, tokens, clock):
    clock.set(datetime(2026, 1, 31, 10, tzinfo=UTC))
    accepted = calibrate(
        client,
        tokens,
        pool="kimi-code",
        window="month",
        percent=10,
        resets_at="2026-02-01T00:00:00+00:00",
    )
    assert accepted.status_code == 200
    rejected = calibrate(
        client,
        tokens,
        pool="kimi-code",
        window="month",
        percent=10,
        resets_at="2026-03-01T00:00:00+00:00",
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["message"] == "reset is more than one month window ahead"


def test_calibrate_checks_past_before_rolling(client, tokens):
    response = calibrate(
        client,
        tokens,
        pool="kimi-code",
        window="5h",
        percent=10,
        resets_at="2000-01-01T00:00:00+00:00",
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "reset time is already in the past"
