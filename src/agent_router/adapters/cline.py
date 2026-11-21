"""Cline CLI adapter: JSONL output, provider/model split, and prompt after `--`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_router.adapters.base import (
    EMPTY_OUTPUT,
    MAX_ARG_CHARS,
    RunOutcome,
    as_int,
    executable,
    json_lines,
    looks_like_limit,
    settle,
)
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable


def file_phrase(prompt_file: Path) -> str:
    return f"Read the task from the file {prompt_file.as_posix()} and execute it exactly."


def _error_details(event: dict[str, Any]) -> tuple[str, bool]:
    kind = event.get("type")
    if kind == "error":
        message = event.get("message")
        return (message if isinstance(message, str) else ""), event.get(
            "errorClass"
        ) == "rate_limit"
    if kind != "agent_event":
        return "", False
    nested = event.get("event")
    if not isinstance(nested, dict) or nested.get("type") != "error":
        return "", False
    error = nested.get("error")
    error = error if isinstance(error, dict) else {}
    message = error.get("message")
    rate_limit = event.get("errorClass") == "rate_limit" or nested.get("errorClass") == "rate_limit"
    return (message if isinstance(message, str) else ""), rate_limit


class ClineAdapter:
    name = "cline"
    reports_progress = True

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        provider, separator, name = model.partition("/")
        if not separator or not provider or not name:
            raise ValueError("cline model must be <provider>/<model>")
        argv = [executable(exe), "--json", "-P", provider, "-m", name]
        argv += ["--plan"] if mode == "read" else ["--auto-approve", "true"]
        by_file = exe.is_shim or len(text) > MAX_ARG_CHARS
        argv += ["--", file_phrase(prompt_file) if by_file else text]
        return Invocation(tuple(argv))

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        events, other = json_lines(stdout)
        result: dict[str, Any] | None = None
        session_id: str | None = None
        top_level_error = ""
        nested_error = ""
        limit_hint = False
        for event in events:
            kind = event.get("type")
            if kind is None:
                continue
            if kind == "run_result":
                result = event
            elif kind == "hook_event" and session_id is None:
                task_id = event.get("taskId")
                if isinstance(task_id, str):
                    session_id = task_id
            message, rate_limit = _error_details(event)
            limit_hint = limit_hint or rate_limit
            if kind == "error" and not top_level_error:
                top_level_error = message
            elif kind == "agent_event" and not nested_error:
                nested_error = message

        usage = result.get("usage") if result is not None else None
        usage = usage if isinstance(usage, dict) else {}
        raw_text = result.get("text") if result is not None else None
        text = raw_text if isinstance(raw_text, str) else ""
        failed = exit_code != 0 or (result is not None and result.get("finishReason") == "error")
        harness_error = (text or top_level_error or nested_error) if failed else None
        if result is None and harness_error is None:
            return RunOutcome("error", error=EMPTY_OUTPUT, session_id=session_id)
        cost = usage.get("totalCost")
        service = list(other)
        if harness_error:
            service.append(harness_error)
            limit_hint = limit_hint or looks_like_limit(harness_error)
        return settle(
            exit_code=1 if failed and exit_code == 0 else exit_code,
            text=text,
            service=service,
            stderr=stderr,
            limit_hint=limit_hint,
            harness_error=harness_error,
            tokens_in=as_int(usage.get("inputTokens")) if "inputTokens" in usage else None,
            tokens_out=as_int(usage.get("outputTokens")) if "outputTokens" in usage else None,
            cost_usd=float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0
            else None,
            session_id=session_id,
        )

    def is_final_line(self, line: str) -> bool:
        events, _ = json_lines(line)
        return bool(events) and events[0].get("type") == "run_result"


HARNESS = HarnessSpec(
    "cline", ClineAdapter(), ("cline", "cline-cli"), spend="journal", proxy_capable=False
)
