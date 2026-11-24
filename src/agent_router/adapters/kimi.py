"""Kimi Code CLI adapter: `kimi -p <text> -m <model> --output-format stream-json`."""

from __future__ import annotations

import json
import re
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
_USAGE_RECORD = '"usage.record"'
_SESSION_ID = re.compile(r"session_[0-9A-Za-z-]{1,64}")


def file_phrase(prompt_file: Path) -> str:
    """Short prompt reference: the job itself lives in a file inside the job directory."""
    return f"Read the task from the file {prompt_file.as_posix()} and execute it exactly."


class KimiAdapter:
    """Read mode is enforced by the prompt preamble: `--plan` is incompatible with `-p`."""

    name = "kimi"
    reports_progress = True
    # Kimi prints one JSON line per finished message: while the model writes a long answer
    # or a subagent works, nothing reaches the output, so CPU time and changed files count
    # as activity too
    quiet_while_working = True

    def __init__(self, kimi_home: Path | None = None) -> None:
        self.kimi_home = Path.home() / ".kimi-code" if kimi_home is None else kimi_home

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
        try:
            usage = self._usage(session_id) if session_id is not None else None
        except Exception:
            usage = None
        tokens_in, tokens_out = usage or (None, None)
        return settle(
            exit_code=exit_code,
            text=text,
            service=service,
            stderr=stderr,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            session_id=session_id,
        )

    def _usage(self, session_id: str) -> tuple[int, int] | None:
        if _SESSION_ID.fullmatch(session_id) is None:
            return None
        tokens_in = tokens_out = 0
        found = False
        try:
            session_dirs = tuple(self.kimi_home.glob(f"sessions/*/{session_id}"))
        except (OSError, ValueError):
            return None
        for session_dir in session_dirs:
            try:
                wire_files = tuple(session_dir.glob("agents/*/wire.jsonl"))
            except (OSError, ValueError):
                continue
            for wire_file in wire_files:
                try:
                    with wire_file.open(encoding="utf-8", errors="replace") as lines:
                        for line in lines:
                            if _USAGE_RECORD not in line:
                                continue
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(event, dict) or event.get("type") != "usage.record":
                                continue
                            usage = event.get("usage")
                            if not isinstance(usage, dict):
                                continue
                            values = tuple(
                                usage.get(key, 0)
                                for key in (
                                    "inputOther",
                                    "inputCacheRead",
                                    "inputCacheCreation",
                                    "output",
                                )
                            )
                            if any(
                                not isinstance(value, int) or isinstance(value, bool) or value < 0
                                for value in values
                            ):
                                continue
                            tokens_in += sum(values[:3])
                            tokens_out += values[3]
                            found = True
                except OSError:
                    continue
        return (tokens_in, tokens_out) if found else None

    def is_final_line(self, line: str) -> bool:
        if _RESUME_HINT not in line:
            return False
        events, _ = json_lines(line)
        return bool(events) and events[0].get("type") == _RESUME_HINT


HARNESS = HarnessSpec("kimi", KimiAdapter(), ("kimi",), spend="journal")
