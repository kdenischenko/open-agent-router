"""Kimi Code CLI adapter: `kimi -p <text> -m <model> --output-format stream-json`."""

from __future__ import annotations

from pathlib import Path

from agent_router.adapters.base import (
    MAX_ARG_CHARS,
    RunOutcome,
    executable,
    json_lines,
    settle,
)
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable

_RESUME_HINT = "session.resume_hint"


def file_phrase(prompt_file: Path) -> str:
    """Short prompt reference: the job itself lives in a file inside the job directory."""
    return f"Read the task from the file {prompt_file.as_posix()} and execute it exactly."


class KimiAdapter:
    """Read mode is enforced by the prompt preamble: `--plan` is incompatible with `-p`."""

    name = "kimi"

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        argv = [executable(exe), "-p"]
        by_file = exe.is_shim or len(text) > MAX_ARG_CHARS
        argv.append(file_phrase(prompt_file) if by_file else text)
        argv += ["-m", model, "--output-format", "stream-json"]
        if by_file:
            argv += ["--add-dir", prompt_file.parent.as_posix()]
        return Invocation(tuple(argv))

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        events, other = json_lines(stdout)
        text = ""
        session_id: str | None = None
        service = list(other)
        for event in events:
            role = event.get("role")
            if role == "assistant":
                content = event.get("content")
                if isinstance(content, str) and content.strip():
                    text = content
            elif role == "meta":
                kind = str(event.get("type", ""))
                if kind == _RESUME_HINT and isinstance(event.get("session_id"), str):
                    session_id = event["session_id"]
                elif "error" in kind.lower():
                    service.append(str(event.get("content") or event.get("message") or kind))
        return settle(
            exit_code=exit_code, text=text, service=service, stderr=stderr, session_id=session_id
        )

    def is_final_line(self, line: str) -> bool:
        if _RESUME_HINT not in line:
            return False
        events, _ = json_lines(line)
        return bool(events) and events[0].get("type") == _RESUME_HINT


HARNESS = HarnessSpec("kimi", KimiAdapter(), ("kimi",), spend="journal")
