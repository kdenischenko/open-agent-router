"""Print Claude Code limits and forward them to the local gateway."""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

NO_DATA = "limits: no data"
SIGNAL_PATH = "/api/v1/signals/claude-statusline"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib forwards the Authorization header to the redirect target: a foreign
    # process on the gateway port would leak the client token to another node
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _urlopen(request: urllib.request.Request, timeout: float) -> Any:
    return _OPENER.open(request, timeout=timeout)


def _percentage(window: Any) -> str | None:
    if not isinstance(window, dict):
        return None
    value = window.get("used_percentage")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0 <= value <= 100:
        return None
    return f"{value:g}"


def render(rate_limits: Any) -> str:
    if not isinstance(rate_limits, dict):
        return NO_DATA
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        percentage = _percentage(rate_limits.get(key))
        if percentage is not None:
            parts.append(f"{label} {percentage}%")
    return " · ".join(parts) if parts else NO_DATA


def post_signal(url: str, token: str, rate_limits: Any, timeout: float = 1.0) -> None:
    try:
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            return
        request = urllib.request.Request(
            url.rstrip("/") + SIGNAL_PATH,
            data=json.dumps({"rate_limits": rate_limits}, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        _urlopen(request, timeout=timeout).close()
    except Exception:
        return


def _home(argv: list[str] | None) -> Path | None:
    args = sys.argv[1:] if argv is None else argv
    home = None
    for index, arg in enumerate(args[:-1]):
        if arg == "--home":
            home = args[index + 1]
            break
    if home is None:
        home = os.environ.get("AGENT_ROUTER_HOME")
    return Path(home).expanduser() if home else None


def _base_url(home: Path) -> str:
    # the same override the client commands honor, e.g. after serve --port
    url = os.environ.get("AGENT_ROUTER_URL", "").strip()
    if url:
        return url
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    server = config.get("server", {})
    port = server.get("port", 8787) if isinstance(server, dict) else 8787
    if isinstance(port, bool) or not isinstance(port, int):
        port = 8787
    return f"http://127.0.0.1:{port}"


def _send(argv: list[str] | None, rate_limits: Any) -> None:
    try:
        home = _home(argv)
        if home is None:
            return
        token = (home / "client.token").read_text(encoding="utf-8").strip()
        if token:
            post_signal(_base_url(home), token, rate_limits)
    except Exception:
        return


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        payload = {}
    rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None

    if rate_limits is not None:
        _send(argv, rate_limits)

    try:
        output = render(rate_limits)
    except Exception:
        output = NO_DATA
    try:
        sys.stdout.buffer.write(f"{output}\n".encode())
        sys.stdout.buffer.flush()
    except Exception:
        return 0
    return 0
