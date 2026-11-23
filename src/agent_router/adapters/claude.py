"""Claude Code adapter: `claude -p --output-format stream-json --verbose`, prompt via stdin.

The output format was verified live with Claude Code CLI 2.1.330 on Windows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_router.adapters.base import RunOutcome, as_int, executable, json_lines, settle
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable

_PERMISSION = {"read": "plan", "write": "bypassPermissions"}
_INPUT_TOKENS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _result_object(
    stdout: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """Result object from one legacy JSON document or the stream's result event."""
    try:
        whole = json.loads(stdout)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return whole, [whole], []
    events, other = json_lines(stdout)
    for event in reversed(events):
        if event.get("type") == "result":
            return event, events, other
    return None, events, other


def _last_rate_limit(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info")
            return info if isinstance(info, dict) else {}
    return {}


def _limit_reset(rate_limit: dict[str, Any]) -> datetime | None:
    if rate_limit.get("status") != "rejected":
        return None
    value = rate_limit.get("resetsAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class ClaudeAdapter:
    name = "claude"
    reports_progress = True

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        argv = [
            executable(exe),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--permission-mode",
            _PERMISSION[mode],
            "--setting-sources",
            "project",
            "--strict-mcp-config",
        ]
        return Invocation(tuple(argv), stdin_text=text)

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        result, events, service = _result_object(stdout)
        if result is None:
            return settle(exit_code=exit_code, text="", service=service, stderr=stderr)
        raw = result.get("result")
        answer = raw if isinstance(raw, str) else ""
        failed = result.get("is_error") is True
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        cost = result.get("total_cost_usd")
        session = result.get("session_id")
        rate_limit = _last_rate_limit(events)
        limit_hint = (
            result.get("api_error_status") == 429
            or result.get("api_error_type") == "rate_limit_error"
            or rate_limit.get("status") == "rejected"
        )
        harness_error: str | None = None
        if failed or limit_hint:
            # with is_error, the result field holds the harness's message, not the model's reply
            harness_error = answer.strip() or str(result.get("subtype") or "harness error")
            service.append(harness_error)
        tokens_in = (
            sum(as_int(usage.get(key)) for key in _INPUT_TOKENS)
            if any(key in usage for key in _INPUT_TOKENS)
            else None
        )
        return settle(
            exit_code=1 if limit_hint and exit_code == 0 else exit_code,
            text="" if failed else answer,
            service=service,
            stderr=stderr,
            limit_hint=limit_hint,
            harness_error=harness_error,
            tokens_in=tokens_in,
            tokens_out=as_int(usage.get("output_tokens")) if "output_tokens" in usage else None,
            cost_usd=float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None,
            session_id=session if isinstance(session, str) else None,
            limit_reset_at=_limit_reset(rate_limit),
        )

    def is_final_line(self, line: str) -> bool:
        events, _ = json_lines(line)
        return bool(events) and events[0].get("type") == "result"


HARNESS = HarnessSpec("claude", ClaudeAdapter(), ("claude",), spend="journal")
