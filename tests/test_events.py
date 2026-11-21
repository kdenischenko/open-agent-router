from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_router import events
from agent_router.clock import FakeClock
from agent_router.events import Event, EventBus

T0 = datetime(2026, 11, 12, 10, tzinfo=UTC)


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def bus(clock):
    bus = EventBus(clock)
    yield bus
    bus.close(timeout=1)


class Collector:
    """Subscriber that collects events; can wait until a given count arrives."""

    def __init__(self):
        self.events: list[Event] = []
        self._cond = threading.Condition()

    def __call__(self, event: Event) -> None:
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def wait_for(self, count: int, timeout: float = 5) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: len(self.events) >= count, timeout)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_sequence_numbers_and_event_fields(bus, clock):
    assert bus.seq == 0
    first = bus.publish("observation.added", {"pool": "p", "window": "5h"})
    clock.advance(timedelta(seconds=5))
    second = bus.publish("counter.sampled")
    third = bus.publish("observation.updated", {})
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert bus.seq == 3
    assert first == Event(1, "observation.added", T0, {"pool": "p", "window": "5h"})
    assert second.at == T0 + timedelta(seconds=5)
    assert second.data == {}


def test_empty_type_rejected(bus):
    with pytest.raises(ValueError):
        bus.publish("")
    assert bus.seq == 0


def test_wait_returns_at_once_when_seq_already_greater(bus):
    bus.publish("a")
    bus.publish("a")
    started = time.monotonic()
    assert bus.wait(0, timeout=5) == 2
    assert bus.wait(1, timeout=30) == 2
    assert time.monotonic() - started < 20  # liveness: we did not wait out the full timeout


def test_wait_wakes_on_publish_from_other_thread(bus):
    bus.publish("a")
    timer = threading.Timer(0.1, bus.publish, args=("b",))
    timer.start()
    started = time.monotonic()
    try:
        assert bus.wait(1, timeout=5) == 2
    finally:
        timer.join()
    assert time.monotonic() - started < 4


def test_wait_times_out_with_old_seq(bus):
    bus.publish("a")
    started = time.monotonic()
    assert bus.wait(1, timeout=0.1) == 1
    assert time.monotonic() - started >= 0.09


def test_subscriber_gets_events_in_order(bus):
    got = Collector()
    bus.subscribe("one", got)
    for n in range(20):
        bus.publish("tick", {"n": n})
    assert got.wait_for(20)
    assert [event.seq for event in got.events] == list(range(1, 21))
    assert [event.data["n"] for event in got.events] == list(range(20))
    assert wait_until(lambda: bus.stats()["one"]["delivered"] == 20)
    assert bus.stats() == {"one": {"delivered": 20, "dropped": 0, "errors": 0}}


def test_events_published_before_subscribe_are_not_delivered(bus):
    bus.publish("old")
    got = Collector()
    bus.subscribe("late", got)
    bus.publish("new")
    assert got.wait_for(1)
    assert [event.type for event in got.events] == ["new"]


def test_duplicate_subscriber_name_rejected(bus):
    bus.subscribe("one", lambda event: None)
    with pytest.raises(ValueError):
        bus.subscribe("one", lambda event: None)


def test_handler_exception_does_not_stop_delivery(bus):
    good = Collector()
    seen = Collector()

    def flaky(event: Event) -> None:
        seen(event)
        if event.seq == 1:
            raise RuntimeError("handler failure")

    bus.subscribe("flaky", flaky)
    bus.subscribe("good", good)
    for _ in range(3):
        bus.publish("tick")
    assert seen.wait_for(3) and good.wait_for(3)
    assert [event.seq for event in seen.events] == [1, 2, 3]
    assert [event.seq for event in good.events] == [1, 2, 3]
    assert wait_until(lambda: bus.stats()["flaky"]["delivered"] == 2)
    assert bus.stats()["flaky"] == {"delivered": 2, "dropped": 0, "errors": 1}
    assert bus.stats()["good"]["errors"] == 0


def test_stuck_subscriber_does_not_block_publisher(clock):
    bus = EventBus(clock, queue_size=2)
    release = threading.Event()
    entered = threading.Event()
    fast = Collector()

    def stuck(event: Event) -> None:
        entered.set()
        release.wait(timeout=30)

    bus.subscribe("stuck", stuck)
    bus.subscribe("fast", fast)
    try:
        started = time.monotonic()
        for _ in range(10):
            bus.publish("tick")
        elapsed = time.monotonic() - started
        assert not release.is_set()  # publish calls returned while the subscriber is blocked
        assert elapsed < 20  # liveness: the subscriber holds the handler for 30 s
        assert bus.seq == 10
        assert bus.stats()["stuck"]["dropped"] >= 7
        assert entered.wait(5)
    finally:
        release.set()
        assert bus.close(timeout=5) == []
    assert bus.stats()["stuck"]["delivered"] + bus.stats()["stuck"]["dropped"] == 10


def test_non_json_data_is_type_error(bus):
    with pytest.raises(TypeError):
        bus.publish("a", {"when": T0})
    with pytest.raises(TypeError):
        bus.publish("a", {"items": {1, 2}})
    with pytest.raises(TypeError):
        bus.publish("a", ["not", "a dict"])
    assert bus.seq == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_value_error(bus, value):
    with pytest.raises(ValueError):
        bus.publish("a", {"x": value})
    assert bus.seq == 0


@pytest.mark.parametrize("key", ["prompt", "text", "output", "token", "cwd", "path"])
def test_forbidden_key_at_any_level(bus, key):
    with pytest.raises(ValueError):
        bus.publish("a", {key: "x"})
    with pytest.raises(ValueError):
        bus.publish("a", {"job": {"meta": [{key: "x"}]}})
    assert bus.seq == 0


def test_data_is_copied_at_publish(bus):
    got = Collector()
    release = threading.Event()

    def handler(event: Event) -> None:
        release.wait(timeout=5)
        got(event)

    bus.subscribe("one", handler)
    data = {"job": {"state": "queued"}, "n": [1, 2]}
    event = bus.publish("job.changed", data)
    data["job"]["state"] = "done"
    data["n"].append(3)
    release.set()
    assert got.wait_for(1)
    assert got.events[0].data == {"job": {"state": "queued"}, "n": [1, 2]}
    assert event.data == {"job": {"state": "queued"}, "n": [1, 2]}


def test_close_delivers_the_rest_and_is_repeatable(clock):
    bus = EventBus(clock)
    got: list[int] = []

    def slow(event: Event) -> None:
        time.sleep(0.01)
        got.append(event.seq)

    bus.subscribe("slow", slow)
    for _ in range(20):
        bus.publish("tick")
    assert bus.close(timeout=5) == []
    assert got == list(range(1, 21))
    assert bus.stats()["slow"] == {"delivered": 20, "dropped": 0, "errors": 0}
    assert bus.close() == []
    assert bus.close(timeout=0) == []


def test_publish_after_close_is_silent(clock):
    bus = EventBus(clock)
    got = Collector()
    bus.subscribe("one", got)
    bus.publish("before")
    assert bus.close(timeout=5) == []
    event = bus.publish("after")
    assert event.seq == 2
    assert bus.seq == 2
    assert bus.wait(1, timeout=0) == 2  # wait still sees the new sequence number
    assert [item.type for item in got.events] == ["before"]
    assert bus.stats()["one"] == {"delivered": 1, "dropped": 0, "errors": 0}
    with pytest.raises(RuntimeError):
        bus.subscribe("late", got)


def test_close_reports_stuck_subscriber(clock):
    bus = EventBus(clock)
    release = threading.Event()
    entered = threading.Event()

    def stuck(event: Event) -> None:
        entered.set()
        release.wait(timeout=30)

    bus.subscribe("stuck", stuck)
    bus.subscribe("fine", lambda event: None)
    try:
        for _ in range(3):
            bus.publish("tick")
        assert entered.wait(5)
        started = time.monotonic()
        assert bus.close(timeout=0.2) == ["stuck"]
        assert time.monotonic() - started < 20  # liveness, not a speed threshold
        assert bus.stats()["stuck"]["dropped"] == 2  # undelivered events count as dropped
        assert bus.stats()["fine"]["delivered"] == 3
    finally:
        release.set()
    assert wait_until(lambda: bus.close(timeout=1) == [])


def test_four_threads_publish_distinct_consecutive_numbers(clock):
    bus = EventBus(clock)
    got = Collector()
    bus.subscribe("one", got)
    numbers: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(4)

    def publisher() -> None:
        start.wait(timeout=10)
        for _ in range(100):
            event = bus.publish("tick")
            with guard:
                numbers.append(event.seq)

    threads = [threading.Thread(target=publisher) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(numbers) == list(range(1, 401))
    assert got.wait_for(400)
    seen = [event.seq for event in got.events]
    assert seen == sorted(seen) and len(set(seen)) == 400
    assert bus.close(timeout=5) == []


def test_close_from_handler_does_not_join_itself(clock):
    bus = EventBus(clock)
    result: list[list[str]] = []
    done = threading.Event()

    def closer(event: Event) -> None:
        result.append(bus.close(timeout=1))
        done.set()

    bus.subscribe("closer", closer)
    bus.publish("tick")
    try:
        assert done.wait(10)
        assert result == [["closer"]]  # we don't join our own thread: it is still in the handler
        assert bus.stats()["closer"]["errors"] == 0
    finally:
        bus.close()


def test_failed_thread_start_leaves_no_subscriber(clock, monkeypatch):
    bus = EventBus(clock)

    def no_start(self) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", no_start)
        with pytest.raises(RuntimeError):
            bus.subscribe("ghost", lambda event: None)
    assert bus.stats() == {}
    assert bus.close() == []  # the bus has no unstarted thread, so join does not fail


def test_subscriber_cannot_change_data_for_others(bus):
    mutated = threading.Event()
    other = Collector()

    def mutator(event: Event) -> None:
        event.data["job"]["state"] = "changed"
        event.data["items"].append(2)
        mutated.set()

    bus.subscribe("mutator", mutator)
    bus.subscribe("other", other)
    published = bus.publish("job.changed", {"job": {"state": "queued"}, "items": [1]})

    assert mutated.wait(5)
    assert other.wait_for(1)
    assert other.events[0].data == {"job": {"state": "queued"}, "items": [1]}
    assert published.data == {"job": {"state": "queued"}, "items": [1]}


def test_returned_event_cannot_change_subscriber_data(bus):
    """Sanity check: a green run proves no regression fix — it is also green on the
    pre-fix code.

    Copying outside the lock is proven by `test_copy_runs_outside_bus_lock`.
    """
    entered = threading.Event()
    release = threading.Event()
    got = Collector()

    def handler(event: Event) -> None:
        entered.set()
        release.wait(timeout=10)
        got(event)

    bus.subscribe("one", handler)
    published = bus.publish("job.changed", {"job": {"state": "queued"}})
    try:
        assert entered.wait(10)
        published.data["job"]["state"] = "changed"
    finally:
        release.set()

    assert got.wait_for(1, timeout=10)
    assert got.events[0].data == {"job": {"state": "queued"}}


def test_copy_runs_outside_bus_lock(bus, monkeypatch: pytest.MonkeyPatch) -> None:
    """Data is copied outside the bus lock: a subscribe from another thread does
    not block meanwhile.

    Regression: previously the per-subscriber copy was taken under the lock, so a
    subscribe from another thread could not complete within 2 s.
    """
    bus.subscribe("first", Collector())
    original = events.copy.deepcopy
    publisher = threading.get_ident()
    subscribed: list[bool] = []

    def deepcopy_with_subscribe(value, *args, **kwargs):
        if threading.get_ident() == publisher and not subscribed:
            late = threading.Thread(target=bus.subscribe, args=("late", Collector()), daemon=True)
            late.start()
            late.join(timeout=2)
            subscribed.append(not late.is_alive())
        return original(value, *args, **kwargs)

    monkeypatch.setattr(events.copy, "deepcopy", deepcopy_with_subscribe)
    published = bus.publish("job.changed", {"job": "j-1"})

    assert published.data == {"job": "j-1"}
    assert subscribed == [True]


@pytest.mark.parametrize(
    "key",
    [
        "apikey",
        "user_passwd",
        "Cookie",
        "credentials",
        "accesstoken",
        "refreshToken",
        "sessiontoken",
        "session_cookie",
    ],
)
def test_forbidden_key_substrings(bus, key):
    with pytest.raises(ValueError):
        bus.publish("x", {key: 1})
    assert bus.seq == 0


@pytest.mark.parametrize("key", ["tokens_in", "tokens_out", "tokens", "session_id", "sessions"])
def test_usage_keys_are_allowed(bus, key):
    bus.publish("x", {key: 1})
    assert bus.seq == 1


def test_deepcopy_runs_outside_bus_lock(bus, monkeypatch):
    locked_states = []
    original_deepcopy = events.copy.deepcopy

    def checked_deepcopy(value):
        locked_states.append(bus._lock.locked())
        return original_deepcopy(value)

    monkeypatch.setattr(events.copy, "deepcopy", checked_deepcopy)
    got = Collector()
    bus.subscribe("one", got)
    bus.publish("tick", {"job": {"state": "queued"}})

    assert got.wait_for(1, timeout=10)
    assert locked_states
    assert not any(locked_states)


@pytest.mark.parametrize(
    ("data", "allowed"),
    [
        ({"access_token": "x"}, False),
        ({"apiKey": "x"}, False),
        ({"Prompt": "x"}, False),
        ({"log_path": "x"}, False),
        ({"a": [{"secret": 1}]}, False),
        ({"context": "x"}, True),
        ({"error_code": "x"}, True),
        ({"from": "x"}, True),
        ({"until": "x"}, True),
        ({"reason": "x"}, True),
    ],
)
def test_forbidden_key_parts(bus, data, allowed):
    if allowed:
        bus.publish("event", data)
        assert bus.seq == 1
    else:
        with pytest.raises(ValueError):
            bus.publish("event", data)
        assert bus.seq == 0


@pytest.mark.parametrize(
    "key",
    [
        "job",
        "from",
        "route",
        "pool",
        "error_code",
        "n",
        "until",
        "reason",
        "window",
        "source",
        "fraction",
    ],
)
def test_existing_event_types_still_publish(bus, key):
    event = bus.publish("existing.event", {key: "value"})
    assert event.data == {key: "value"}


def test_abandoned_subscriber_closes_after_handler_returns(clock):
    bus = EventBus(clock)
    entered = threading.Event()
    release = threading.Event()
    abandoned = threading.Event()
    calls = []

    def handler(event: Event) -> None:
        entered.set()
        release.wait(timeout=30)

    def on_abandoned() -> None:
        calls.append(None)
        abandoned.set()

    bus.subscribe("stuck", handler, on_abandoned=on_abandoned)
    bus.publish("tick")
    try:
        assert entered.wait(5)
        assert bus.close(timeout=0.1) == ["stuck"]
        assert not abandoned.is_set()
        release.set()
        assert abandoned.wait(5)
        assert wait_until(lambda: bus.close(timeout=1) == [])
        assert len(calls) == 1
    finally:
        release.set()
        bus.close(timeout=5)


def test_abandoned_callback_exception_is_suppressed(clock):
    bus = EventBus(clock)
    entered = threading.Event()
    release = threading.Event()
    called = threading.Event()
    calls = []

    def handler(event: Event) -> None:
        entered.set()
        release.wait(timeout=30)

    def on_abandoned() -> None:
        calls.append(None)
        called.set()
        raise RuntimeError("close failure")

    bus.subscribe("stuck", handler, on_abandoned=on_abandoned)
    bus.publish("tick")
    try:
        assert entered.wait(5)
        assert bus.close(timeout=0.1) == ["stuck"]
        release.set()
        assert called.wait(5)
        assert wait_until(lambda: bus.close(timeout=1) == [])
        assert len(calls) == 1
    finally:
        release.set()
        bus.close(timeout=5)
