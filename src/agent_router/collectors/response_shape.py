"""Redacted response-shape logging for malformed usage-source answers."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

log = logging.getLogger(__name__)

_EVENT_TYPE_KEYS = frozenset({"type", "event_type", "eventType"})
_EVENT_TYPES = frozenset(
    {
        "assistant",
        "content_block_delta",
        "content_block_start",
        "content_block_stop",
        "error",
        "message",
        "message_delta",
        "message_start",
        "message_stop",
        "ping",
        "result",
        "stream_event",
        "system",
        "text",
        "user",
    }
)


def _shape(value: object, key: str = "") -> object:
    if isinstance(value, Mapping):
        return {str(name): _shape(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return {"list_length": len(value), "items": [_shape(item) for item in value]}
    if isinstance(value, str):
        if key in _EVENT_TYPE_KEYS and value in _EVENT_TYPES:
            return value
        return {"string_length": len(value)}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return type(value).__name__


def _json_lines_shape(raw: bytes | str) -> object | None:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = []
    parsed_any = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            lines.append({"parsed": False, "bytes": len(line.encode("utf-8"))})
        else:
            parsed_any = True
            lines.append(_shape(event))
    return {"lines": lines} if parsed_any else None


def log_malformed_response(source: str, raw: bytes | str, *, json_lines: bool = False) -> None:
    """Log answer metadata without answer values; raw size is always its UTF-8 byte size."""
    byte_length = len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))
    try:
        body = json.loads(raw)
    except ValueError:
        shape = _json_lines_shape(raw) if json_lines else None
    else:
        shape = _shape(body)
    if shape is None:
        log.warning(
            "usage source %s returned malformed_response: bytes=%d parsed=false",
            source,
            byte_length,
        )
        return
    log.warning(
        "usage source %s returned malformed_response: bytes=%d shape=%s",
        source,
        byte_length,
        json.dumps(shape, sort_keys=True),
    )
