"""Shared helpers for harness adapters: run outcome, preamble, and limit detection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from agent_router.config import Mode
from agent_router.runner import Invocation, ResolvedExecutable

MAX_ARG_CHARS = 20_000
ERROR_MAX_CHARS = 500
EMPTY_OUTPUT = "empty output"

_RULES = (
    "- Work only inside the current working directory.",
    "- Do not run `git commit`, `git push`, or any command that rewrites git history.",
    "- Do not start long-running background processes.",
    "- When finished, reply with a concise final report of what you did or found.",
)
_READ_RULE = "- This is a read-only task: do not create, modify, or delete any files."

_LIMIT_SIGNS = re.compile(
    r"rate[ _-]?limit|usage[ _-]?limit|quota|limit reached|hit your limit|"
    r"too many requests|(?<!\d)429(?!\d)",
    re.IGNORECASE,
)
_RESET_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})"
)


@dataclass(frozen=True)
class RunOutcome:
    status: Literal["ok", "error", "limit"]
    text: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    limit_reset_at: datetime | None = None
    error: str | None = None


class Adapter(Protocol):
    name: str
    reports_progress: bool  # whether stdout/stderr show progress while the job runs

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation: ...

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome: ...

    def is_final_line(self, line: str) -> bool: ...

    # optional: `is_progress_line(line) -> bool` tells the runner which stdout lines reset the
    # idle limit; an adapter without it counts all output as progress

    # optional: `quiet_while_working: bool` (False when absent) marks a harness that stays
    # silent while it works; the runner then counts CPU time and changed files as activity,
    # so such a harness is not stopped as idle while it is still doing something


def with_preamble(prompt: str, mode: Mode) -> str:
    """Prompt prefixed with executor rules; read mode adds a ban on changing files."""
    lines = ["[agent-router] You are executing a delegated task for an orchestrator.", "Rules:"]
    lines.extend(_RULES)
    if mode == "read":
        lines.append(_READ_RULE)
    lines.append("---")
    return "\n".join(lines) + "\n" + prompt


def looks_like_limit(text: str) -> bool:
    """Whether a harness service message looks like hitting the subscription limit."""
    return bool(_LIMIT_SIGNS.search(text))


def reset_time(text: str) -> datetime | None:
    """Limit reset time from a message: the first ISO 8601 timestamp with a timezone."""
    for match in _RESET_AT.finditer(text):
        raw = match.group(0).replace(" ", "T")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            found = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if found.tzinfo is not None:
            return found
    return None


def executable(exe: ResolvedExecutable) -> str:
    if exe.path is None:
        raise ValueError("harness executable not found")
    return str(exe.path)


def json_lines(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Output lines split into JSON objects and everything else (harness service messages)."""
    events: list[dict[str, Any]] = []
    other: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            other.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            other.append(line)
    return events, other


def as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= ERROR_MAX_CHARS else text[-ERROR_MAX_CHARS:]


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def settle(
    *,
    exit_code: int | None,
    text: str,
    service: list[str],
    stderr: str,
    limit_hint: bool = False,
    harness_error: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    session_id: str | None = None,
    limit_reset_at: datetime | None = None,
) -> RunOutcome:
    """Shared outcome resolution order.

    Zero exit code with non-empty text is always ok. Otherwise limit, if limit signs appear
    in the service messages (`service`, stderr) or the harness itself reported the error as
    a limit (`limit_hint`); otherwise error. The model response text is not checked for
    limit signs.
    """
    common = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "session_id": session_id,
    }
    if exit_code == 0 and text.strip():
        return RunOutcome("ok", text=text, **common)
    notes = "\n".join([*service, stderr])
    if harness_error:
        message = harness_error
    elif exit_code == 0:
        message = EMPTY_OUTPUT
    else:
        message = _last_line(stderr) or _last_line("\n".join(service))
        if not message:
            message = "process did not exit" if exit_code is None else f"exit code {exit_code}"
    if limit_hint or looks_like_limit(notes):
        return RunOutcome(
            "limit",
            text=text,
            limit_reset_at=limit_reset_at or reset_time(notes),
            error=_short(message),
            **common,
        )
    return RunOutcome("error", text=text, error=_short(message), **common)
