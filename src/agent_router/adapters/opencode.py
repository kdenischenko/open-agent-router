"""OpenCode adapter: `opencode run --format json --archived --standalone --model <model>`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_router.adapters.base import RunOutcome, as_int, executable, json_lines, settle
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable


def _part(event: dict[str, Any]) -> dict[str, Any]:
    part = event.get("part")
    return part if isinstance(part, dict) else {}


class OpenCodeAdapter:
    """Read mode uses the plan agent (creates no files), write mode passes `--auto`."""

    name = "opencode"
    reports_progress = True

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        argv = [
            executable(exe),
            "run",
            "--format",
            "json",
            "--archived",
            "--standalone",
            "--model",
            model,
        ]
        argv += ["--agent", "plan"] if mode == "read" else ["--auto"]
        return Invocation(tuple(argv), stdin_text=text)

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        events, other = json_lines(stdout)
        texts: list[tuple[object, str]] = []
        answers: set[object] = set()  # messages that ended a turn, not led to a tool call
        tokens_in = tokens_out = 0
        cost = 0.0
        steps = 0
        session_id: str | None = None
        harness_error: str | None = None
        limit_hint = False
        model_unavailable_hint = False
        service = list(other)
        for event in events:
            kind = event.get("type")
            if session_id is None and isinstance(event.get("sessionID"), str):
                session_id = event["sessionID"]
            part = _part(event)
            if kind == "text":
                if isinstance(part.get("text"), str):
                    texts.append((part.get("messageID"), part["text"]))
            elif kind == "step_finish":
                if part.get("reason") == "stop":
                    answers.add(part.get("messageID"))
                tokens = part.get("tokens")
                if isinstance(tokens, dict):
                    steps += 1
                    tokens_in += as_int(tokens.get("input"))
                    tokens_out += as_int(tokens.get("output")) + as_int(tokens.get("reasoning"))
                value = part.get("cost")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost += float(value)
            elif kind == "error":
                error = event.get("error")
                error = error if isinstance(error, dict) else {}
                error_type = str(error.get("type", ""))
                message = str(error.get("message", "")) or error_type or "harness error"
                harness_error = f"{error_type}: {message}" if error_type else message
                service.append(harness_error)
                lowered = error_type.lower()
                limit_hint = limit_hint or "limit" in lowered or "quota" in lowered
                model_unavailable_hint = (
                    model_unavailable_hint
                    or "no-route" in lowered
                    or "model unavailable" in message.lower()
                )
        if texts:
            # the last message counts even when the run stopped before its step finished
            answers.add(texts[-1][0])
        text = "\n".join(chunk for message, chunk in texts if message in answers)
        return settle(
            exit_code=exit_code,
            text=text,
            service=service,
            stderr=stderr,
            limit_hint=limit_hint,
            harness_error=harness_error,
            model_unavailable_hint=model_unavailable_hint,
            tokens_in=tokens_in if steps else None,
            tokens_out=tokens_out if steps else None,
            cost_usd=cost if cost > 0 else None,
            session_id=session_id,
        )

    def is_final_line(self, line: str) -> bool:
        return False

    def is_progress_line(self, line: str) -> bool:
        events, _ = json_lines(line)
        return not events or events[0].get("type") != "retry"


HARNESS = HarnessSpec("opencode", OpenCodeAdapter(), ("opencode",), spend="counter")
