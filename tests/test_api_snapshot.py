from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from agent_router import __version__
from agent_router.api import create_app
from agent_router.events import EventBus
from agent_router.policy import PoolBlock
from agent_router.service import (
    SnapshotCache,
    advise,
    build_snapshot,
    decision_json,
    route_views,
    snapshot_json,
)
from agent_router.store import CounterSample, Observation
from helpers import BASE_URL, bearer, full_config_text, read_only_route, write_config

POOL_FIELDS = {
    "id",
    "title",
    "unit",
    "known",
    "lag",
    "urgency",
    "blocked_by",
    "blocked_until",
    "reserve_hit",
    "block",
    "via_gateway",
    "counter_sampled_at",
    "usage",
    "windows",
}
WINDOW_FIELDS = {
    "id",
    "kind",
    "role",
    "known",
    "used",
    "expected",
    "lag",
    "urgency",
    "resets_at",
    "source",
    "figure_source",
    "observed_at",
    "age_s",
    "estimated",
    "estimated_prices",
    "blocked",
    "note",
    "anchored",
}


# --- access -----------------------------------------------------------------------


def test_healthz_needs_no_token(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_snapshot_requires_token(client, tokens):
    response = client.get("/api/v1/snapshot")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/snapshot", headers=bearer("garbage")).status_code == 401
    assert (
        client.get("/api/v1/snapshot", headers={"Authorization": tokens.client}).status_code == 401
    )
    assert client.get("/api/v1/snapshot", headers=bearer(tokens.client)).status_code == 200
    assert client.get("/api/v1/snapshot", headers=bearer(tokens.admin)).status_code == 200


def test_foreign_host_rejected(client, tokens):
    headers = bearer(tokens.admin) | {"Host": "evil.example"}
    response = client.get("/api/v1/snapshot", headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_host"
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/healthz", headers={"Host": "localhost:9999"}).status_code == 200


def test_unknown_path_is_json_404(client, tokens):
    response = client.get("/api/v1/nope", headers=bearer(tokens.client))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- snapshot ---------------------------------------------------------------------


def test_snapshot_has_three_pools_and_fields(client, tokens, clock):
    body = client.get("/api/v1/snapshot", headers=bearer(tokens.client)).json()
    assert body["now"] == clock.now().isoformat()
    assert [pool["id"] for pool in body["pools"]] == ["claude-pro", "kimi-code", "opencode-go"]
    for pool in body["pools"]:
        assert set(pool) == POOL_FIELDS
        assert pool["known"] is False and pool["lag"] is None
        assert pool["block"] is None and pool["via_gateway"] is None
        for window in pool["windows"]:
            assert set(window) == WINDOW_FIELDS
            assert window["known"] is False and window["note"]
    assert [window["id"] for window in body["pools"][2]["windows"]] == ["5h", "week", "month"]


def test_snapshot_window_has_anchored_flag(config, store, clock):
    pool = config.pools[0]
    anchored = replace(pool.windows[0], anchor=clock.now())
    anchored_config = replace(
        config,
        pools=(replace(pool, windows=(anchored, *pool.windows[1:])), *config.pools[1:]),
    )
    snapshot = build_snapshot(anchored_config, store, clock.now(), UTC)
    windows = snapshot_json(snapshot, store)["pools"][0]["windows"]
    assert [window["anchored"] for window in windows] == [True, False]


def test_snapshot_figure_source_and_usage_without_a_source(config, store, clock):
    now = clock.now()
    store.add_observation(Observation("claude-pro", "week", 0.3, now, None, "manual"))
    snapshot = build_snapshot(config, store, now, UTC)
    pools = {pool["id"]: pool for pool in snapshot_json(snapshot, store, config=config)["pools"]}

    claude = pools["claude-pro"]
    # the template config has no [pools.usage]: every pool reports no usage source at all
    assert all(pool["usage"] is None for pool in pools.values())
    week = claude["windows"][1]
    assert week["figure_source"] == "manual"
    assert claude["windows"][0]["figure_source"] == "journal"  # no observation in that window


def test_snapshot_figure_source_is_journal_after_a_scheduled_reset(config, store, clock):
    now = clock.now()
    # a reset in the past turns the observation into a scheduled reset with zero usage
    store.add_observation(
        Observation("claude-pro", "week", 0.3, now - timedelta(days=2), now, "manual")
    )
    snapshot = build_snapshot(config, store, now, UTC)
    pools = {pool["id"]: pool for pool in snapshot_json(snapshot, store, config=config)["pools"]}

    week = pools["claude-pro"]["windows"][1]
    assert week["source"] == "scheduled_reset"
    assert week["figure_source"] == "journal"


def test_snapshot_json_values(config, store, clock):
    now = clock.now()
    reset = now + timedelta(days=3, hours=12)  # half of the weekly window
    store.add_observation(
        Observation("claude-pro", "week", 0.30, now - timedelta(minutes=5), reset, "manual")
    )
    store.add_observation(
        Observation("opencode-go", "5h", 0.95, now - timedelta(hours=1), None, "manual")
    )
    store.add_counter_sample(CounterSample("opencode-go", 4.0, now - timedelta(minutes=2)))
    block = PoolBlock("kimi-code", now + timedelta(hours=1), "hit the limit")
    stale = PoolBlock("claude-pro", now, "expired")
    snapshot = build_snapshot(config, store, now, UTC, {"kimi-code": block, "claude-pro": stale})
    assert set(snapshot.blocks) == {"kimi-code"}
    pools = {pool["id"]: pool for pool in snapshot_json(snapshot, store)["pools"]}

    week = pools["claude-pro"]["windows"][1]
    assert week["used"] == 0.30 and week["expected"] == 0.5
    assert abs(week["lag"] - 0.2) < 1e-9
    assert week["resets_at"] == reset.isoformat()
    assert week["source"] == "manual" and week["age_s"] == 300.0
    assert week["observed_at"] == (now - timedelta(minutes=5)).isoformat()
    assert pools["claude-pro"]["known"] is True
    assert abs(pools["claude-pro"]["lag"] - 0.2) < 1e-9
    assert pools["claude-pro"]["counter_sampled_at"] is None

    assert pools["kimi-code"]["block"] == {
        "until": (now + timedelta(hours=1)).isoformat(),
        "reason": "hit the limit",
    }
    go = pools["opencode-go"]
    assert go["blocked_by"] == "5h"
    assert go["blocked_until"] == (now + timedelta(hours=4)).isoformat()
    assert go["windows"][0]["blocked"] is True
    assert go["counter_sampled_at"] == (now - timedelta(minutes=2)).isoformat()
    json.dumps(snapshot_json(snapshot, store), allow_nan=False)


def test_advise_and_decision_json(config, store, clock):
    now = clock.now()
    reset = now + timedelta(days=15)
    store.add_observation(Observation("kimi-code", "month", 0.2, now, reset, "manual"))
    snapshot = build_snapshot(config, store, now, UTC)
    body = decision_json("standard", advise(config, snapshot, "standard", None))
    assert body["tier"] == "standard"
    assert [item["route"] for item in body["chosen"]] == [
        "kimi-cli/k3.1-pro",
        "kimi-cli/kimi-for-coding",
        "claude-cli/sonnet",
        "opencode-cli/glm-5.3",
    ]
    assert set(body["chosen"][0]) == {"route", "pool", "reason"}
    excluded = {item["route"]: item["reason"] for item in body["excluded"]}
    assert excluded == {
        "cline/glm-5.3": "route disabled",
        "opencode-cli/glm-5.3-flash": "does not serve tier standard",
    }
    from_kimi = advise(config, snapshot, "standard", "kimi-code")
    assert [item.route.id for item in from_kimi.chosen] == [
        "claude-cli/sonnet",
        "opencode-cli/glm-5.3",
    ]
    assert {item.reason for item in from_kimi.excluded} >= {"caller pool"}
    unknown_caller = advise(config, snapshot, "standard", "no-such-caller")
    assert len(unknown_caller.chosen) == 4
    assert body["mode"] is None


def test_advise_with_a_mode_excludes_routes_without_it(home, store, clock):
    config = write_config(home, read_only_route(full_config_text(), "kimi-cli/k3.1-pro"))
    snapshot = build_snapshot(config, store, clock.now(), UTC)

    write = advise(config, snapshot, "standard", None, "write")
    read = advise(config, snapshot, "standard", None, "read")

    excluded = {item.route.id: item.reason for item in write.excluded}
    assert excluded["kimi-cli/k3.1-pro"] == "mode write not supported"
    assert "kimi-cli/k3.1-pro" in [item.route.id for item in read.chosen]
    assert decision_json("standard", write, "write")["mode"] == "write"


def test_route_views_availability(config):
    views = route_views(config, {"kimi-cli/k3.1-pro": "kimi not found", "claude-cli/sonnet": None})
    by_id = {view.route.id: view for view in views}
    assert len(views) == len(config.routes)
    assert by_id["kimi-cli/k3.1-pro"].available is False
    assert by_id["kimi-cli/k3.1-pro"].unavailable_reason == "kimi not found"
    assert by_id["claude-cli/sonnet"].available is True
    assert all(view.available for view in route_views(config))


# --- snapshot cache -----------------------------------------------------------------


def make_cache(bus, clock, ttl_s=1.0):
    calls: list[datetime] = []

    def build(now):
        calls.append(now)
        return object()

    return SnapshotCache(bus, clock, ttl_s, build), calls


def test_cache_reuses_snapshot_until_event_or_ttl(bus, clock):
    cache, calls = make_cache(bus, clock)
    first = cache.get()
    assert cache.get() is first
    assert len(calls) == 1
    bus.publish("observation.added", {"pool": "p"})
    second = cache.get()
    assert second is not first and cache.get() is second
    assert len(calls) == 2
    clock.advance(timedelta(seconds=0.9))
    assert cache.get() is second
    clock.advance(timedelta(seconds=0.1))
    assert cache.get() is not second
    assert len(calls) == 3
    assert calls[-1] == clock.now()


def test_cache_rebuilds_when_event_arrives_during_build(bus, clock):
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def build(now):
        calls.append(bus.seq)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return object()

    cache = SnapshotCache(bus, clock, 60.0, build)
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(cache.get()))
    thread.start()
    assert entered.wait(5)
    bus.publish("observation.added", {"pool": "p"})  # observation arrived while the snapshot built
    release.set()
    thread.join(5)
    assert len(results) == 1
    second = cache.get()  # clock did not move, but the event came after the build started
    assert second is not results[0]
    assert calls == [0, 1]
    assert cache.get() is second


def test_snapshot_endpoint_uses_cache(client, tokens, store, clock):
    headers = bearer(tokens.client)
    first = client.get("/api/v1/snapshot", headers=headers).json()
    store.add_observation(
        Observation(
            "claude-pro", "week", 0.5, clock.now(), clock.now() + timedelta(days=1), "manual"
        )
    )
    cached = client.get("/api/v1/snapshot", headers=headers).json()
    assert cached == first  # no event, TTL not expired
    clock.advance(timedelta(seconds=1))
    fresh = client.get("/api/v1/snapshot", headers=headers).json()
    assert fresh["pools"][0]["windows"][1]["used"] == 0.5


def test_repeated_snapshot_does_not_touch_store(client, tokens, store, monkeypatch):
    """A repeated snapshot does not touch the store when no new events occurred."""
    headers = bearer(tokens.client)
    first = client.get("/api/v1/snapshot", headers=headers).json()
    calls: list[str] = []
    for name in ("last_counter", "last_observation", "observations"):
        real = getattr(store, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(store, name, spy)
    assert client.get("/api/v1/snapshot", headers=headers).json() == first
    assert calls == []


# --- events and sinks -----------------------------------------------------------------


def test_event_stats(client, tokens, bus):
    assert client.get("/api/v1/events/stats").status_code == 401
    bus.subscribe("probe", lambda event: None)
    bus.publish("observation.added", {"pool": "p"})
    body = client.get("/api/v1/events/stats", headers=bearer(tokens.client)).json()
    assert body["seq"] == 1
    assert set(body["subscribers"]) == {"probe"}
    assert set(body["subscribers"]["probe"]) == {"delivered", "dropped", "errors"}


def test_app_creates_own_bus_and_closes_everything(config, store, clock, tokens, home):
    app = create_app(config, store, clock, tokens, home=home, background=False)
    bus = app.state.gateway.bus
    assert isinstance(bus, EventBus)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/healthz").status_code == 200
    seen = []
    bus_closed = False
    try:
        bus.subscribe("late", seen.append)
    except RuntimeError:
        bus_closed = True
    assert bus_closed


def test_jsonl_sink_from_config_gets_events(store, clock, tokens, home):
    text = full_config_text().replace('counter = "opencode_stats"\n', "")
    text += '\n[[sinks]]\ntype = "jsonl"\npath = "events.jsonl"\n'
    config = write_config(home, text)
    assert config.sinks and all(pool.counter is None for pool in config.pools)
    app = create_app(config, store, clock, tokens, home=home, background=True)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/healthz").status_code == 200
        app.state.gateway.bus.publish("observation.added", {"pool": "claude-pro", "window": "5h"})
        stats = client.get("/api/v1/events/stats", headers=bearer(tokens.client)).json()
        assert list(stats["subscribers"]) == ["sink:jsonl:0"]
    lines = (home / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["seq"] == 1 and record["type"] == "observation.added"
    assert record["data"] == {"pool": "claude-pro", "window": "5h"}


def test_background_false_attaches_no_sinks(store, clock, tokens, home):
    text = full_config_text() + '\n[[sinks]]\ntype = "jsonl"\npath = "events.jsonl"\n'
    config = write_config(home, text)
    app = create_app(config, store, clock, tokens, home=home, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        stats = client.get("/api/v1/events/stats", headers=bearer(tokens.client)).json()
    assert stats["subscribers"] == {}
    assert not (home / "events.jsonl").exists()
