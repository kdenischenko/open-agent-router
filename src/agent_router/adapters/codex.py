"""OpenAI Codex CLI adapter: `codex exec --json`, with the prompt passed via stdin."""

from __future__ import annotations

from pathlib import Path

from agent_router.adapters.base import RunOutcome, as_int, executable, json_lines, settle
from agent_router.config import Mode
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ResolvedExecutable

_SANDBOX = {"read": "read-only", "write": "workspace-write"}


class CodexAdapter:
    name = "codex"
    reports_progress = True
    # Codex is silent while the model reasons, so CPU time and changed files count as activity too
    quiet_while_working = True

    def build(
        self, text: str, mode: Mode, model: str, exe: ResolvedExecutable, prompt_file: Path
    ) -> Invocation:
        argv = [
            executable(exe),
            "exec",
            "--json",
            "--skip-git-repo-check",
            # the user config may register MCP servers, this gateway among them, and plugins
            "--ignore-user-config",
            # without a sandbox setting Codex on Windows rejects every shell command; values
            # stay unquoted because .cmd shims refuse quotes and Codex reads them as strings
            "-c",
            "windows.sandbox=unelevated",
            "-c",
            "approval_policy=never",
            "-m",
            model,
            "-s",
            _SANDBOX[mode],
            "-",
        ]
        return Invocation(tuple(argv), stdin_text=text)

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        events, service = json_lines(stdout)
        text = ""
        session_id: str | None = None
        tokens_in: int | None = None
        tokens_out: int | None = None
        harness_error: str | None = None
        for event in events:
            kind = event.get("type")
            if kind == "thread.started":
                value = event.get("thread_id")
                if isinstance(value, str):
                    session_id = value
            elif kind == "item.completed":
                item = event.get("item")
                item = item if isinstance(item, dict) else {}
                value = item.get("text")
                if item.get("type") == "agent_message" and isinstance(value, str):
                    text = value
            elif kind == "error":
                message = event.get("message")
                if isinstance(message, str) and message:
                    service.append(message)
                    harness_error = message
            elif kind == "turn.failed":
                error = event.get("error")
                error = error if isinstance(error, dict) else {}
                message = error.get("message")
                if isinstance(message, str) and message:
                    service.append(message)
                    harness_error = message
            elif kind == "turn.completed":
                usage = event.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                if "input_tokens" in usage:
                    tokens_in = as_int(usage.get("input_tokens"))
                if "output_tokens" in usage:
                    tokens_out = as_int(usage.get("output_tokens"))
        return settle(
            exit_code=exit_code,
            text=text,
            service=service,
            stderr=stderr,
            harness_error=harness_error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            session_id=session_id,
        )

    def is_final_line(self, line: str) -> bool:
        events, _ = json_lines(line)
        return bool(events) and events[0].get("type") in ("turn.completed", "turn.failed")

    def is_progress_line(self, line: str) -> bool:
        events, _ = json_lines(line)
        if not events:
            return False
        kind = events[0].get("type")
        return isinstance(kind, str) and kind.startswith(("item.", "turn."))


HARNESS = HarnessSpec("codex", CodexAdapter(), ("codex",), spend="journal", min_version="0.172.0")
