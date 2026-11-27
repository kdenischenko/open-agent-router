"""Webhook event sink: config validation, delivery, filtering and failure handling."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_router.config import ConfigError, SinkSettings
from agent_router.events import Event
from agent_router.sinks import open_sink
from agent_router.sinks_webhook import WebhookSink

T0 = datetime(2026, 11, 12, 10, tzinfo=UTC)
ENV = "AGENT_ROUTER_TEST_WEBHOOK_URL"


class FakeReceiver:
    """A local HTTP server that records requests and answers with scripted status codes."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                receiver.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status = receiver.statuses.pop(0) if receiver.statuses else 200
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/topic"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[FakeReceiver]:
    server = FakeReceiver()
    yield server
    server.close()


def event(kind: str = "job.completed", **data: Any) -> Event:
    payload = {"job": "j_1", "from": "running", "route": "a/main", "pool": "pool-a"}
    payload.update(data)
    return Event(seq=7, type=kind, at=T0, data=payload)


def settings(**options: Any) -> SinkSettings:
    return SinkSettings(type="webhook", options={"url_env": ENV, **options})


def sink_for(receiver: FakeReceiver, **options: Any) -> WebhookSink:
    return WebhookSink(
        url=receiver.url,
        events=options.pop("events", None),
        fmt=options.pop("fmt", "json"),
        base_url=options.pop("base_url", None),
        timeout_s=2.0,
        retries=options.pop("retries", 1),
        sleep=lambda seconds: None,
    )


# --- config validation ---------------------------------------------------------------


def test_open_sink_reads_the_url_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiver: FakeReceiver
) -> None:
    monkeypatch.setenv(ENV, receiver.url)

    sink = open_sink(settings(events=["job.completed"], format="text"), tmp_path)
    try:
        assert isinstance(sink, WebhookSink)
        assert sink.url == receiver.url
        assert sink.events == frozenset({"job.completed"})
        assert sink.fmt == "text"
    finally:
        sink.close()


def test_unset_environment_variable_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV, raising=False)

    with pytest.raises(ConfigError, match=ENV):
        open_sink(settings(), tmp_path)


@pytest.mark.parametrize(
    "options",
    [
        {"url_env": ""},
        {"url_env": 5},
        {"format": "xml"},
        {"events": "job.completed"},
        {"events": [1]},
        {"timeout_s": 0},
        {"url": "http://example.invalid/secret"},
    ],
    ids=[
        "empty-env",
        "env-not-text",
        "format",
        "events-not-list",
        "event-not-text",
        "timeout",
        "url",
    ],
)
def test_invalid_webhook_options_are_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    monkeypatch.setenv(ENV, "http://127.0.0.1:9/topic")

    with pytest.raises(ConfigError, match='sink "webhook"'):
        open_sink(settings(**options), tmp_path)


def test_non_http_url_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "file:///etc/passwd")

    with pytest.raises(ConfigError, match='sink "webhook"'):
        open_sink(settings(), tmp_path)


# --- delivery ----------------------------------------------------------------------------


def test_json_format_posts_the_event_payload(receiver: FakeReceiver) -> None:
    sink = sink_for(receiver, base_url="http://127.0.0.1:8765")

    sink.handle(event())

    (request,) = receiver.requests
    assert request["path"] == "/topic"
    assert request["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(request["body"])
    assert body["type"] == "job.completed"
    assert body["seq"] == 7
    assert body["at"] == T0.isoformat()
    assert body["data"]["job"] == "j_1"
    assert body["url"] == "http://127.0.0.1:8765/jobs/j_1"


def test_json_format_without_base_url_has_no_link(receiver: FakeReceiver) -> None:
    sink_for(receiver).handle(event())

    assert "url" not in json.loads(receiver.requests[0]["body"])


def test_text_format_posts_one_line_with_a_title(receiver: FakeReceiver) -> None:
    sink = sink_for(receiver, fmt="text")

    sink.handle(event("job.failed", error_code="executor_error"))

    (request,) = receiver.requests
    assert request["headers"]["Content-Type"].startswith("text/plain")
    assert request["headers"]["Title"] == "agent-router: job.failed"
    text = request["body"].decode("utf-8")
    assert "\n" not in text
    assert "j_1" in text and "a/main" in text and "executor_error" in text


def test_events_filter_skips_other_events(receiver: FakeReceiver) -> None:
    sink = sink_for(receiver, events=frozenset({"job.completed"}))

    sink.handle(event("job.running"))
    sink.handle(event("job.completed"))

    assert len(receiver.requests) == 1


def test_without_a_filter_every_event_is_posted(receiver: FakeReceiver) -> None:
    sink = sink_for(receiver)

    sink.handle(event("job.running"))
    sink.handle(event("pool.blocked", pool="pool-a"))

    assert len(receiver.requests) == 2


# --- failures ----------------------------------------------------------------------------


def test_server_error_is_retried(receiver: FakeReceiver) -> None:
    receiver.statuses = [500]
    sink = sink_for(receiver, retries=1)

    sink.handle(event())

    assert len(receiver.requests) == 2
    assert sink.failures == 0


def test_failed_delivery_is_counted_and_never_raises(receiver: FakeReceiver) -> None:
    receiver.statuses = [500, 500]
    sink = sink_for(receiver, retries=1)

    sink.handle(event())

    assert len(receiver.requests) == 2
    assert sink.failures == 1


def test_unreachable_receiver_is_counted_and_never_raises() -> None:
    sink = WebhookSink(
        url="http://127.0.0.1:9/topic",
        events=None,
        fmt="json",
        base_url=None,
        timeout_s=0.5,
        retries=0,
        sleep=lambda seconds: None,
    )

    sink.handle(event())

    assert sink.failures == 1


def test_failure_log_does_not_reveal_the_url(caplog: pytest.LogCaptureFixture) -> None:
    sink = WebhookSink(
        url="http://127.0.0.1:9/secret-token-topic",
        events=None,
        fmt="json",
        base_url=None,
        timeout_s=0.5,
        retries=0,
        sleep=lambda seconds: None,
    )

    sink.handle(event())

    assert "secret-token-topic" not in caplog.text
