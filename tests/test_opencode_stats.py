from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_router.collectors.opencode_stats as opencode_stats_module
from agent_router import api
from agent_router.api import create_app
from agent_router.collectors.opencode_stats import CounterSampler, counter_from_stats, run_stats
from agent_router.config import BackendSection
from agent_router.runner import KILL_WAIT_S, ResolvedExecutable
from helpers import BASE_URL, bearer

SAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "opencode_stats.json").read_text(encoding="utf-8")
)
OBSERVATIONS = "/api/v1/observations"


def configured_app(config, store, clock, tokens, home, bus, run, *, background=False):
    configured = replace(
        config,
        backends=dict(config.backends)
        | {"opencode": BackendSection("opencode", "opencode", "fake-opencode")},
    )
    app = create_app(configured, store, clock, tokens, home=home, bus=bus, background=background)
    app.state.counter_sampler.resolve = lambda name: ResolvedExecutable(Path(name), False)
    app.state.counter_sampler.run = run
    return app


def calibrate(client, tokens):
    return client.post(
        OBSERVATIONS,
        json={"pool": "opencode-go", "window": "month", "percent": 10, "resets_in": "10d"},
        headers=bearer(tokens.admin),
    )


def month_window(client, tokens):
    body = client.get("/api/v1/snapshot", headers=bearer(tokens.client)).json()
    pool = next(item for item in body["pools"] if item["id"] == "opencode-go")
    return next(item for item in pool["windows"] if item["id"] == "month")


def test_counter_sums_provider_costs():
    assert counter_from_stats(SAMPLE, "opencode-go") == pytest.approx(4.0)


def test_counter_model_selects_one_of_two_provider_models():
    assert counter_from_stats(SAMPLE, "opencode-go", "glm-5.3") == pytest.approx(2.75)


def test_missing_models_returns_none_and_does_not_write_sample(config, store, clock, bus):
    stats = {"cost": 12.5}
    sampler = CounterSampler(config, store, clock, bus, run=lambda executable: stats)

    assert counter_from_stats(stats, "opencode-go") is None
    assert sampler.sample_once() == 0
    assert store.last_counter("opencode-go") is None


def _fake_popen(monkeypatch, responses, *, returncode=0, kill_error=None):
    processes = []

    class FakePopen:
        pid = 4242

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.returncode = returncode
            self.responses = iter(responses)
            self.communicate_timeouts = []
            self.wait_timeouts = []
            self.killed = False
            processes.append(self)

        def communicate(self, timeout):
            self.communicate_timeouts.append(timeout)
            response = next(self.responses)
            if isinstance(response, Exception):
                raise response
            return response

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            if kill_error is not None:
                raise kill_error

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self.returncode

    monkeypatch.setattr(opencode_stats_module.subprocess, "Popen", FakePopen)
    return processes


def test_run_stats_unexpected_error_kills_process_tree_and_waits(monkeypatch):
    processes = _fake_popen(monkeypatch, [RuntimeError("stats failed")], returncode=None)
    killed = []
    monkeypatch.setattr(opencode_stats_module, "kill_tree", killed.append)

    with pytest.raises(RuntimeError, match="stats failed"):
        run_stats("opencode", timeout_s=1)

    assert killed == [4242]
    assert processes[0].killed is False
    assert processes[0].wait_timeouts == [KILL_WAIT_S]


def test_run_stats_cleanup_error_preserves_original_exception(monkeypatch):
    _fake_popen(
        monkeypatch,
        [RuntimeError("stats failed")],
        returncode=None,
        kill_error=OSError("kill failed"),
    )

    def cleanup_fails(_pid):
        raise OSError("cleanup failed")

    monkeypatch.setattr(opencode_stats_module, "kill_tree", cleanup_fails)

    with pytest.raises(RuntimeError, match="stats failed"):
        run_stats("opencode", timeout_s=1)


def test_run_stats_timeout_kills_process_tree(monkeypatch):
    processes = _fake_popen(
        monkeypatch,
        [subprocess.TimeoutExpired("opencode", 1), (b"", b"")],
    )
    killed = []
    monkeypatch.setattr(opencode_stats_module, "kill_tree", killed.append, raising=False)

    assert run_stats("opencode", timeout_s=1) is None
    assert killed == [4242]
    assert processes[0].communicate_timeouts == [1, KILL_WAIT_S]


def test_run_stats_repeated_timeout_kills_process_and_drains_pipes(monkeypatch):
    processes = _fake_popen(
        monkeypatch,
        [
            subprocess.TimeoutExpired("opencode", 1),
            subprocess.TimeoutExpired("opencode", KILL_WAIT_S),
            (b"", b""),
        ],
    )
    monkeypatch.setattr(opencode_stats_module, "kill_tree", lambda pid: None, raising=False)

    assert run_stats("opencode", timeout_s=1) is None
    assert len(processes) == 1
    process = processes[0]
    assert process.killed
    assert process.communicate_timeouts == [1, KILL_WAIT_S, KILL_WAIT_S]


@pytest.mark.parametrize(
    ("platform", "option", "expected"),
    [
        ("win32", "creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
        ("linux", "start_new_session", True),
    ],
)
def test_run_stats_sets_platform_process_options(monkeypatch, platform, option, expected):
    processes = _fake_popen(monkeypatch, [(b'{"models": []}', b"")])
    monkeypatch.setattr(sys, "platform", platform)

    assert run_stats("opencode") == {"models": []}
    assert len(processes) == 1
    process = processes[0]
    assert process.args == (["opencode", "stats", "--json", "--cost", "--models", "--all"],)
    assert process.kwargs["stdin"] == subprocess.DEVNULL
    assert process.kwargs["stdout"] == subprocess.PIPE
    assert process.kwargs["stderr"] == subprocess.PIPE
    assert process.kwargs[option] == expected


def test_run_stats_returns_none_for_nonzero_exit(monkeypatch):
    _fake_popen(monkeypatch, [(b'{"models": []}', b"")], returncode=1)

    assert run_stats("opencode") is None


def test_run_stats_returns_none_for_nonexistent_executable(tmp_path):
    executable = tmp_path / "no-such-opencode"
    assert run_stats(str(executable), timeout_s=1) is None


def test_run_stats_reads_json_from_substitute_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("stats").write_text(
        "import json\n"
        "print(json.dumps({'models': [{'model': {'id': 'm', 'providerID': 'p'}, "
        "'cost': 1.5}]}))\n",
        encoding="utf-8",
    )

    assert run_stats(sys.executable, timeout_s=10) == {
        "models": [{"model": {"id": "m", "providerID": "p"}, "cost": 1.5}]
    }


def test_run_stats_returns_none_for_invalid_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("stats").write_text("print('not json')\n", encoding="utf-8")

    assert run_stats(sys.executable, timeout_s=10) is None


def test_calibration_adds_counter_sample(config, store, clock, tokens, home, bus):
    app = configured_app(config, store, clock, tokens, home, bus, lambda executable: SAMPLE)

    with TestClient(app, base_url=BASE_URL) as client:
        assert calibrate(client, tokens).status_code == 200
        sample = store.last_counter("opencode-go")
        assert sample is not None
        assert sample.value_usd == pytest.approx(4.0)
        assert sample.sampled_at == clock.now()


def test_none_run_writes_nothing_and_publishes_nothing(config, store, clock, bus):
    sampler = CounterSampler(config, store, clock, bus, run=lambda executable: None)
    before = bus.seq

    assert sampler.sample_once() == 0
    assert store.last_counter("opencode-go") is None
    assert bus.seq == before


def test_two_samples_grow_snapshot_and_publish_two_events(config, store, clock, tokens, home, bus):
    increased = copy.deepcopy(SAMPLE)
    increased["models"][0]["cost"] = 7.25
    replies = iter([SAMPLE, increased])
    app = configured_app(config, store, clock, tokens, home, bus, lambda executable: next(replies))
    sampled = []
    both_delivered = threading.Event()

    def collect(event):
        if event.type == "counter.sampled":
            sampled.append(dict(event.data))
            if len(sampled) == 2:
                both_delivered.set()

    bus.subscribe("opencode-test", collect)
    with TestClient(app, base_url=BASE_URL) as client:
        assert calibrate(client, tokens).status_code == 200
        assert month_window(client, tokens)["used"] == pytest.approx(0.10)

        clock.advance(timedelta(seconds=1))
        assert app.state.counter_sampler.sample_once() == 1

        window = month_window(client, tokens)
        assert window["used"] == pytest.approx(0.20)
        assert window["estimated"] is True
        assert both_delivered.wait(10)
        assert sampled == [{"pool": "opencode-go"}, {"pool": "opencode-go"}]


def test_background_false_does_not_start_sampler(config, store, clock, tokens, home, bus):
    calls = []

    def run(executable):
        calls.append(executable)
        return SAMPLE

    app = configured_app(config, store, clock, tokens, home, bus, run, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/healthz").status_code == 200
        assert calls == []
        assert store.last_counter("opencode-go") is None


def test_background_samples_immediately_and_repeats(config, store, clock, tokens, home, bus):
    config = replace(config, counter_interval_s=1)
    first = threading.Event()
    second = threading.Event()
    calls = []

    def run(executable):
        calls.append(executable)
        (first if len(calls) == 1 else second).set()
        return None

    app = configured_app(config, store, clock, tokens, home, bus, run, background=True)
    with TestClient(app, base_url=BASE_URL):
        assert first.wait(10)
        assert second.wait(10)
    assert calls[:2] == ["fake-opencode", "fake-opencode"]


def test_hung_run_does_not_block_app_shutdown(config, store, clock, tokens, home, bus):
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def run(executable):
        started.set()
        release.wait()
        return SAMPLE

    app = configured_app(config, store, clock, tokens, home, bus, run, background=True)
    client = TestClient(app, base_url=BASE_URL)
    client.__enter__()
    closer = threading.Thread(target=lambda: (client.__exit__(None, None, None), stopped.set()))
    closer.daemon = True
    try:
        assert started.wait(10)
        closer.start()
        assert stopped.wait(10)
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(1)


def test_slow_counter_does_not_hold_the_calibration_response(
    config, store, clock, tokens, home, bus, monkeypatch
):
    release = threading.Event()
    sampled = threading.Event()

    def run(executable):
        release.wait(10)
        return SAMPLE

    monkeypatch.setattr(api, "AFTER_CALIBRATION_WAIT_S", 0.05)
    bus.subscribe("opencode-test", lambda event: event.type == "counter.sampled" and sampled.set())
    app = configured_app(config, store, clock, tokens, home, bus, run)
    with TestClient(app, base_url=BASE_URL) as client:
        try:
            response = calibrate(client, tokens)
            pending = store.last_counter("opencode-go")
        finally:
            release.set()
        assert sampled.wait(10)  # the sample still lands once the counter answers
        assert store.last_counter("opencode-go") is not None

    assert response.status_code == 200
    assert pending is None


def test_calibration_survives_counter_failure(config, store, clock, tokens, home, bus):
    def run(executable):
        raise RuntimeError("stats unavailable")

    app = configured_app(config, store, clock, tokens, home, bus, run)
    with TestClient(app, base_url=BASE_URL) as client:
        assert calibrate(client, tokens).status_code == 200
        assert store.last_observation("opencode-go", "month") is not None
        assert store.last_counter("opencode-go") is None
