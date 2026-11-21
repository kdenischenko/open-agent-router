"""Claude Code adapter: `claude -p --output-format json`, prompt passed via stdin.

The output format is taken from the documentation; it has not been verified live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_router.adapters.base import RunOutcome, as_int, executable, json_lines, settle
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable

_PERMISSION = {"read": "plan", "write": "acceptEdits"}


def _result_object(stdout: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Result object: the whole stdout as one JSON document, or the last object line."""
    try:
        whole = json.loads(stdout)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return whole, []
    events, other = json_lines(stdout)
    for event in reversed(events):
        if "result" in event or "is_error" in event:
            return event, other
    return None, other


class ClaudeAdapter:
    name = "claude"

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        argv = [executable(exe), "-p", "--output-format", "json", "--model", model]
        argv += ["--permission-mode", _PERMISSION[mode]]
        return Invocation(tuple(argv), stdin_text=text)

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        result, service = _result_object(stdout)
        if result is None:
            return settle(exit_code=exit_code, text="", service=service, stderr=stderr)
        raw = result.get("result")
        answer = raw if isinstance(raw, str) else ""
        failed = result.get("is_error") is True
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        cost = result.get("total_cost_usd")
        session = result.get("session_id")
        harness_error: str | None = None
        if failed:
            # with is_error, the result field holds the harness's message, not the model's reply
            harness_error = answer.strip() or str(result.get("subtype") or "harness error")
            service = [*service, harness_error]
        return settle(
            exit_code=exit_code,
            text="" if failed else answer,
            service=service,
            stderr=stderr,
            harness_error=harness_error,
            tokens_in=as_int(usage.get("input_tokens")) if "input_tokens" in usage else None,
            tokens_out=as_int(usage.get("output_tokens")) if "output_tokens" in usage else None,
            cost_usd=float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None,
            session_id=session if isinstance(session, str) else None,
        )

    def is_final_line(self, line: str) -> bool:
        return False


HARNESS = HarnessSpec("claude", ClaudeAdapter(), ("claude",), spend="journal")
