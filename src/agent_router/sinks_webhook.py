"""Webhook event sink using only the Python standard library."""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from agent_router.config import ConfigError
from agent_router.events import Event

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class WebhookSink:
    def __init__(
        self,
        url: str,
        events: frozenset[str] | None,
        fmt: str,
        base_url: str | None,
        timeout_s: float,
        retries: int,
        sleep: Callable[[float], None],
    ) -> None:
        self.url = url
        self.events = events
        self.fmt = fmt
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.retries = retries
        self.sleep = sleep
        self.failures = 0
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def handle(self, event: Event) -> None:
        if self.events is not None and event.type not in self.events:
            return
        try:
            body, headers = self._encode(event)
        except Exception:
            self.failures += 1
            logger.warning("webhook event could not be encoded: %s", event.type)
            return

        attempts = max(0, self.retries) + 1
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(
                    self.url, data=body, headers=headers, method="POST"
                )
                with self._opener.open(request, timeout=self.timeout_s) as response:
                    if 200 <= response.status < 300:
                        return
            except Exception:
                pass
            if attempt + 1 < attempts:
                self.sleep(1.0)

        self.failures += 1
        logger.warning("webhook delivery failed for %s after %d attempts", event.type, attempts)

    def _encode(self, event: Event) -> tuple[bytes, dict[str, str]]:
        link = self._job_link(event)
        if self.fmt == "json":
            record: dict[str, Any] = {
                "seq": event.seq,
                "type": event.type,
                "at": event.at.isoformat(),
                "data": event.data,
            }
            if link is not None:
                record["url"] = link
            body = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return body, {"Content-Type": "application/json"}

        action = event.type.partition(".")[2].replace("_", " ") or event.type
        data = event.data
        job = _one_line(data.get("job"))
        route = _one_line(data.get("route"))
        pool = _one_line(data.get("pool"))
        error_code = _one_line(data.get("error_code"))
        if job is not None:
            parts = [f"job {job} {action}"]
        elif pool is not None:
            parts = [f"pool {pool} {action}"]
        else:
            parts = [event.type]
        if route is not None:
            parts.append(f"on {route}")
        if pool is not None and job is not None:
            parts.append(f"({pool})")
        if error_code is not None:
            parts.append(f"error_code={error_code}")
        if link is not None:
            parts.append(link)
        return " ".join(parts).encode("utf-8"), {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": f"agent-router: {event.type}",
        }

    def _job_link(self, event: Event) -> str | None:
        if self.base_url is None:
            return None
        job = event.data.get("job")
        if job is None:
            return None
        return f"{self.base_url.rstrip('/')}/jobs/{job}"

    def close(self) -> None:
        pass


def _one_line(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def open_webhook_sink(options: Mapping[str, Any]) -> WebhookSink:
    prefix = 'sink "webhook":'
    if "url" in options:
        raise ConfigError([f"{prefix} url is not allowed; use url_env"])
    url_env = options.get("url_env")
    if not isinstance(url_env, str) or not url_env:
        raise ConfigError([f"{prefix} url_env must be a non-empty string"])
    url = os.environ.get(url_env)
    if not url:
        raise ConfigError([f"{prefix} environment variable {url_env} is not set"])
    if not _http_url(url):
        raise ConfigError(
            [f"{prefix} environment variable {url_env} must contain an http or https URL"]
        )

    fmt = options.get("format", "json")
    if not isinstance(fmt, str) or fmt not in {"json", "text"}:
        raise ConfigError([f'{prefix} format must be "json" or "text"'])

    raw_events = options.get("events")
    if raw_events is not None and (
        not isinstance(raw_events, list) or not all(isinstance(item, str) for item in raw_events)
    ):
        raise ConfigError([f"{prefix} events must be a list of strings"])
    events = frozenset(raw_events) if raw_events is not None else None

    timeout_s = options.get("timeout_s", 5.0)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ConfigError([f"{prefix} timeout_s must be a positive number"])

    base_url = options.get("base_url")
    if base_url is not None and not _http_url(base_url):
        raise ConfigError([f"{prefix} base_url must be an http or https URL"])

    return WebhookSink(
        url=url,
        events=events,
        fmt=fmt,
        base_url=base_url,
        timeout_s=float(timeout_s),
        retries=1,
        sleep=time.sleep,
    )
