from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_router import plugins
from agent_router.clock import FakeClock
from agent_router.config import (
    ClassifierSettings,
    Config,
    ConfigError,
    SinkSettings,
    StorageSettings,
    TaskSettings,
)
from agent_router.events import Event, EventBus
from agent_router.sinks import JsonlSink, attach_sinks, open_sink

T0 = datetime(2026, 11, 12, 10, tzinfo=UTC)


def event(seq: int, data: dict[str, Any] | None = None) -> Event:
    return Event(seq=seq, type="observation.added", at=T0, data=data or {})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_config(*sinks: SinkSettings) -> Config:
    return Config(
        pools=(),
        routes=(),
        callers={},
        tasks=TaskSettings(allowed_roots=(), denied_roots=()),
        backends={},
        statusline=None,
        classifier=ClassifierSettings(),
        storage=StorageSettings(),
        sinks=sinks,
    )


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[Event] = []
        self.closed = False
        self.fail = fail

    def handle(self, event: Event) -> None:
        if self.fail:
            raise RuntimeError("sink failure")
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class FlakyFile:
    """Real file wrapper that fails flush or close on demand."""

    def __init__(self, file: Any) -> None:
        self._file = file
        self.fail_flush = False
        self.fail_close = False

    def write(self, line: str) -> int:
        return self._file.write(line)

    def flush(self) -> None:
        if self.fail_flush:
            raise OSError("flush denied")
        self._file.flush()

    def close(self) -> None:
        self._file.close()
        if self.fail_close:
            raise OSError("close denied")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)


def test_jsonl_format_and_event_order(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    sink.handle(event(1, {"pool": "pool"}))
    sink.handle(event(2, {"window": "week"}))
    sink.close()

    assert read_jsonl(path) == [
        {
            "seq": 1,
            "type": "observation.added",
            "at": "2026-11-12T10:00:00+00:00",
            "data": {"pool": "pool"},
        },
        {
            "seq": 2,
            "type": "observation.added",
            "at": "2026-11-12T10:00:00+00:00",
            "data": {"window": "week"},
        },
    ]


def test_jsonl_cyrillic_data_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    message = "Привет, мир"  # non-ascii on purpose
    sink.handle(event(1, {"message": message}))
    sink.close()

    assert message in path.read_text(encoding="utf-8")
    assert read_jsonl(path)[0]["data"] == {"message": message}


def test_jsonl_rotates_open_file_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rotated = tmp_path / "events.jsonl.1"
    sink = JsonlSink(path, max_bytes=120)
    try:
        sink.handle(event(1))
        sink.handle(event(2))

        assert [row["seq"] for row in read_jsonl(rotated)] == [1]
        assert [row["seq"] for row in read_jsonl(path)] == [2]
    finally:
        sink.close()


def test_jsonl_failed_rotation_keeps_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    rotated = tmp_path / "events.jsonl.1"
    original_replace = Path.replace
    failed = False

    def fail_replace_once(self: Path, target: Path) -> Path:
        nonlocal failed
        if not failed:
            failed = True
            raise PermissionError("rotation denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace_once)
    sink = JsonlSink(path, max_bytes=120)
    second_error: PermissionError | None = None
    try:
        sink.handle(event(1))
        try:
            sink.handle(event(2))
        except PermissionError as exc:
            second_error = exc
        sink.handle(event(3))

        assert second_error is None
        assert [row["seq"] for row in read_jsonl(rotated)] == [1, 2]
        assert [row["seq"] for row in read_jsonl(path)] == [3]
    finally:
        sink.close()


def test_jsonl_failed_rotation_and_reopen_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    original_open = Path.open

    def fail_replace(self: Path, target: Path) -> Path:
        raise PermissionError("rotation denied")

    def fail_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise OSError("file unavailable")
        return original_open(self, *args, **kwargs)

    sink = JsonlSink(path, max_bytes=120)
    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "open", fail_open)
    try:
        sink.handle(event(1))
        with pytest.raises(OSError):
            sink.handle(event(2))
        with pytest.raises(RuntimeError, match="jsonl sink closed"):
            sink.handle(event(3))
        sink.close()
        with pytest.raises(RuntimeError, match="jsonl sink closed"):
            sink.handle(event(3))
    finally:
        sink.close()


def test_jsonl_rotation_flush_failure_reopens_and_keeps_writing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rotated = tmp_path / "events.jsonl.1"
    sink = JsonlSink(path, max_bytes=120)
    real = sink._file
    assert real is not None
    flaky = FlakyFile(real)
    sink._file = flaky
    try:
        sink.handle(event(1))
        flaky.fail_flush = True
        sink.handle(event(2))

        assert sink._file is not flaky
        assert sink._file is not None
        assert not sink._file.closed
        sink.handle(event(3))
    finally:
        sink.close()

    assert [row["seq"] for row in read_jsonl(rotated)] == [1, 2]
    assert [row["seq"] for row in read_jsonl(path)] == [3]


def test_jsonl_rotation_close_failure_reopens_and_keeps_writing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rotated = tmp_path / "events.jsonl.1"
    sink = JsonlSink(path, max_bytes=120)
    real = sink._file
    assert real is not None
    flaky = FlakyFile(real)
    sink._file = flaky
    try:
        sink.handle(event(1))
        flaky.fail_close = True
        sink.handle(event(2))

        assert sink._file is not flaky
        assert sink._file is not None
        assert not sink._file.closed
        sink.handle(event(3))
    finally:
        sink.close()

    assert [row["seq"] for row in read_jsonl(rotated)] == [1, 2]
    assert [row["seq"] for row in read_jsonl(path)] == [3]


def test_jsonl_rotation_failure_backoffs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "events.jsonl"
    replace_calls = 0
    original_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        nonlocal replace_calls
        if self == path:
            replace_calls += 1
            raise PermissionError("rotation denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    sink = JsonlSink(path, max_bytes=1000)
    try:
        for seq in range(1, 31):
            sink.handle(event(seq))
    finally:
        sink.close()

    assert replace_calls == 2
    assert len(read_jsonl(path)) == 30


def test_relative_jsonl_path_is_resolved_from_home(tmp_path: Path) -> None:
    sink = open_sink(SinkSettings(type="jsonl", options={"path": "events.jsonl"}), tmp_path)
    try:
        assert isinstance(sink, JsonlSink)
        assert sink.path == tmp_path / "events.jsonl"
        assert sink.path.exists()
    finally:
        sink.close()


def test_tilde_in_sink_path_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    sink = open_sink(
        SinkSettings(type="jsonl", options={"path": "~/events.jsonl"}),
        tmp_path / "gateway",
    )
    try:
        assert isinstance(sink, JsonlSink)
        assert sink.path == tmp_path / "events.jsonl"
        assert sink.path.exists()
    finally:
        sink.close()


@pytest.mark.parametrize("options", [{}, {"path": ""}, {"path": 42}])
def test_jsonl_requires_nonempty_string_path(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="jsonl"):
        open_sink(SinkSettings(type="jsonl", options=options), tmp_path)


@pytest.mark.parametrize("max_bytes", [0, -1, 1.5, True, "100"])
def test_jsonl_rejects_invalid_max_bytes(tmp_path: Path, max_bytes: Any) -> None:
    settings = SinkSettings(type="jsonl", options={"path": "events.jsonl", "max_bytes": max_bytes})
    with pytest.raises(ConfigError, match="jsonl"):
        open_sink(settings, tmp_path)


def test_unknown_sink_type_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing-sink"):
        open_sink(SinkSettings(type="missing-sink", options={}), tmp_path)


def test_third_party_sink_uses_plugin_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[tuple[str, str]] = []
    calls: list[tuple[Any, Path]] = []
    expected = RecordingSink()

    def load_plugin(group: str, name: str) -> Any:
        loaded.append((group, name))

        def factory(options: Any, home: Path) -> RecordingSink:
            calls.append((options, home))
            return expected

        return factory

    monkeypatch.setattr(plugins, "load_plugin", load_plugin)
    settings = SinkSettings(type="external", options={"url": "https://example.test"})
    actual = open_sink(settings, tmp_path)

    assert actual is expected
    assert loaded == [("agent_router.sinks", "external")]
    assert calls == [(settings.options, tmp_path)]


def test_third_party_factory_exception_is_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(options: Any, home: Path) -> RecordingSink:
        raise RuntimeError("cannot create")

    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: factory)
    with pytest.raises(ConfigError, match="broken"):
        open_sink(SinkSettings(type="broken", options={}), tmp_path)


def test_second_sink_failure_closes_first_without_subscribing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = RecordingSink()

    def failing_factory(options: Any, home: Path) -> RecordingSink:
        raise OSError("no disk")

    factories = {"first": lambda options, home: first, "second": failing_factory}
    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: factories[name])
    config = make_config(
        SinkSettings(type="first", options={}), SinkSettings(type="second", options={})
    )
    bus = EventBus(FakeClock(T0))
    try:
        with pytest.raises(ConfigError, match="second"):
            attach_sinks(bus, config, tmp_path)
        assert first.closed
        assert bus.stats() == {}
    finally:
        bus.close()


def test_attach_sinks_uses_type_and_config_index_in_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[RecordingSink] = []

    def factory(options: Any, home: Path) -> RecordingSink:
        sink = RecordingSink()
        opened.append(sink)
        return sink

    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: factory)
    config = make_config(
        SinkSettings(type="external", options={}), SinkSettings(type="external", options={})
    )
    bus = EventBus(FakeClock(T0))
    try:
        attach_sinks(bus, config, tmp_path)
        assert list(bus.stats()) == ["sink:external:0", "sink:external:1"]
    finally:
        bus.close()
        for sink in opened:
            sink.close()


def test_failing_sink_does_not_block_neighboring_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = RecordingSink(fail=True)
    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: lambda options, home: failing)
    path = tmp_path / "events.jsonl"
    config = make_config(
        SinkSettings(type="broken", options={}),
        SinkSettings(type="jsonl", options={"path": str(path)}),
    )
    bus = EventBus(FakeClock(T0))
    sinks = attach_sinks(bus, config, tmp_path)
    try:
        published = bus.publish("observation.added", {"pool": "pool"})
        assert bus.close(timeout=5) == []
    finally:
        bus.close()
        for sink in sinks:
            sink.close()

    assert read_jsonl(path) == [
        {
            "seq": published.seq,
            "type": "observation.added",
            "at": "2026-11-12T10:00:00+00:00",
            "data": {"pool": "pool"},
        }
    ]
    assert bus.stats()["sink:broken:0"]["errors"] == 1
    assert bus.stats()["sink:jsonl:1"]["delivered"] == 1


def test_attach_sinks_rollback_closes_every_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CloseFailingSink(RecordingSink):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failure")

    first = CloseFailingSink()
    second = RecordingSink()

    def failing_factory(options: Any, home: Path) -> RecordingSink:
        raise OSError("no disk")

    factories = {
        "first": lambda options, home: first,
        "second": lambda options, home: second,
        "third": failing_factory,
    }
    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: factories[name])
    config = make_config(
        SinkSettings(type="first", options={}),
        SinkSettings(type="second", options={}),
        SinkSettings(type="third", options={}),
    )
    bus = EventBus(FakeClock(T0))
    try:
        with pytest.raises(ConfigError, match="third"):
            attach_sinks(bus, config, tmp_path)
        assert first.closed
        assert second.closed
        assert bus.stats() == {}
    finally:
        bus.close()


def test_stuck_jsonl_sink_file_is_closed_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    original_handle = JsonlSink.handle
    original_close = JsonlSink.close

    def stuck_handle(self: JsonlSink, item: Event) -> None:
        entered.set()
        release.wait(timeout=30)
        original_handle(self, item)

    def close_and_signal(self: JsonlSink) -> None:
        original_close(self)
        closed.set()

    monkeypatch.setattr(JsonlSink, "handle", stuck_handle)
    monkeypatch.setattr(JsonlSink, "close", close_and_signal)
    config = make_config(SinkSettings(type="jsonl", options={"path": "events.jsonl"}))
    bus = EventBus(FakeClock(T0))
    sinks = attach_sinks(bus, config, tmp_path)
    sink = sinks[0]
    assert isinstance(sink, JsonlSink)
    file = sink._file
    assert file is not None
    try:
        bus.publish("observation.added", {"pool": "pool"})
        assert entered.wait(5)
        assert bus.close(timeout=0.1) == ["sink:jsonl:0"]
        assert not file.closed
        release.set()
        assert closed.wait(5)
        assert file.closed
    finally:
        release.set()
        bus.close(timeout=5)
        sink.close()


def test_jsonl_close_waits_for_concurrent_handle(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    handle_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    class BlockingFile:
        def __init__(self, file: Any) -> None:
            self._file = file

        def write(self, line: str) -> int:
            entered.set()
            assert release.wait(10)
            return self._file.write(line)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._file, name)

    sink = JsonlSink(tmp_path / "events.jsonl")
    file = sink._file
    assert file is not None
    sink._file = BlockingFile(file)

    def handle() -> None:
        try:
            sink.handle(event(1, {"pool": "pool"}))
        except BaseException as exc:
            handle_errors.append(exc)

    def close() -> None:
        close_started.set()
        try:
            sink.close()
        except BaseException as exc:
            close_errors.append(exc)

    handler = threading.Thread(target=handle)
    closer = threading.Thread(target=close)
    handler.start()
    assert entered.wait(10)
    closer.start()
    assert close_started.wait(10)
    release.set()
    handler.join(10)
    closer.join(10)

    assert not handler.is_alive()
    assert not closer.is_alive()
    assert handle_errors == []
    assert close_errors == []
    with pytest.raises(RuntimeError, match="jsonl sink closed"):
        sink.handle(event(2))
    sink.close()
