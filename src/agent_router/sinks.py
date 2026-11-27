"""Event sinks: JSONL with rotation and third-party connectors."""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Protocol, TextIO

from agent_router import plugins
from agent_router.config import Config, ConfigError, SinkSettings
from agent_router.events import Event, EventBus
from agent_router.sinks_webhook import open_webhook_sink

SINK_GROUP = "agent_router.sinks"


class EventSink(Protocol):
    def handle(self, event: Event) -> None: ...

    def close(self) -> None: ...


class JsonlSink:
    def __init__(self, path: Path, max_bytes: int = 10_000_000) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._file: TextIO | None = self.path.open("a", encoding="utf-8", newline="\n")
        self._size = self.path.stat().st_size
        self._rotate_limit = max_bytes

    def handle(self, event: Event) -> None:
        with self._lock:
            if self._file is None:
                raise RuntimeError("jsonl sink closed")
            record = {
                "seq": event.seq,
                "type": event.type,
                "at": event.at.isoformat(),
                "data": event.data,
            }
            line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            encoded = line.encode("utf-8")
            if self._size + len(encoded) > self._rotate_limit:
                self._rotate()
            self._file.write(line)
            self._file.flush()
            self._size += len(encoded)

    def _rotate(self) -> None:
        assert self._file is not None
        try:
            self._file.flush()
            self._file.close()
            self.path.replace(Path(f"{self.path}.1"))
            self._file = self.path.open("w", encoding="utf-8", newline="\n")
            self._size = 0
            self._rotate_limit = self.max_bytes
        except (OSError, ValueError):
            # flush, close or replace failed: the old handle may be closed, so reopen
            # for appending and retry rotation only after max_bytes more data
            self._reopen_append()
            self._rotate_limit = self._size + self.max_bytes

    def _reopen_append(self) -> None:
        file = self._file
        self._file = None
        if file is not None:
            with contextlib.suppress(OSError):
                file.close()
        handle = self.path.open("a", encoding="utf-8", newline="\n")
        try:
            self._size = self.path.stat().st_size
        except OSError:
            handle.close()
            raise
        self._file = handle

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def open_sink(settings: SinkSettings, home: Path) -> EventSink:
    if settings.type == "webhook":
        return open_webhook_sink(settings.options)
    if settings.type == "jsonl":
        raw_path = settings.options.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ConfigError(['sink "jsonl": path must be a non-empty string'])
        max_bytes = settings.options.get("max_bytes", 10_000_000)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ConfigError(['sink "jsonl": max_bytes must be an integer greater than 0'])
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path(home) / path
        try:
            return JsonlSink(path, max_bytes=max_bytes)
        except Exception as exc:
            raise ConfigError([f'sink "jsonl" not created: {type(exc).__name__}: {exc}']) from exc
    try:
        factory = plugins.load_plugin(SINK_GROUP, settings.type)
        return factory(settings.options, Path(home))
    except Exception as exc:
        raise ConfigError(
            [f'sink "{settings.type}" not created: {type(exc).__name__}: {exc}']
        ) from exc


def attach_sinks(bus: EventBus, config: Config, home: Path) -> list[EventSink]:
    opened: list[EventSink] = []
    try:
        for settings in config.sinks:
            opened.append(open_sink(settings, home))
    except Exception:
        for sink in opened:
            with contextlib.suppress(Exception):
                sink.close()
        raise
    for index, (settings, sink) in enumerate(zip(config.sinks, opened, strict=True)):
        bus.subscribe(f"sink:{settings.type}:{index}", sink.handle, on_abandoned=sink.close)
    return opened
