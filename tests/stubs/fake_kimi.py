"""Stand-in for the Kimi Code CLI: answers `kimi -p <text> -m <model> --output-format stream-json`.

The reply names the model and repeats the last line of the task, so a test can see that the
prompt, read from the argument or from the file the gateway points to, reached the executor.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FILE_PREFIX = "Read the task from the file "
FILE_SUFFIX = " and execute it exactly."


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def task_text(argv: list[str]) -> str:
    text = option(argv, "-p")
    if text.startswith(FILE_PREFIX) and text.endswith(FILE_SUFFIX):
        path = Path(text[len(FILE_PREFIX) : -len(FILE_SUFFIX)])
        return path.read_text(encoding="utf-8")
    return text


def main(argv: list[str]) -> int:
    lines = [line for line in task_text(argv).splitlines() if line.strip()]
    events = [
        {"role": "meta", "type": "system.version", "version": "0.0.0"},
        {"role": "assistant", "content": f"{option(argv, '-m')} read: {lines[-1]}"},
        {"role": "meta", "type": "session.resume_hint", "session_id": "session_stub"},
    ]
    for event in events:
        sys.stdout.buffer.write((json.dumps(event) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
