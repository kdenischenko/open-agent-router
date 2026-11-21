"""Write the release notes of one version from CHANGELOG.md.

The section under ``## [<version>]`` is the body of the GitHub release. Lines wrapped in the
changelog are joined, because GitHub shows every line break in release notes. A missing or empty
section is an error, so a release is never published without notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]: \S+$")
BLOCK_START = re.compile(r"^(#|- |\* |\d+\. |\||>|```)")
FENCE = "```"


class NotesError(Exception):
    """The changelog has no notes for the version."""


def section(text: str, version: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if start is None:
            if match and match.group("version") == version:
                start = index + 1
        elif match:
            return lines[start:index]
    if start is None:
        raise NotesError(f"changelog has no section for version {version}")
    body = lines[start:]
    while body and (not body[-1].strip() or LINK_DEFINITION.match(body[-1])):
        body.pop()
    return body


def join_wrapped(lines: list[str]) -> list[str]:
    joined: list[str] = []
    in_code = False
    for line in lines:
        if line.lstrip().startswith(FENCE):
            in_code = not in_code
            joined.append(line)
            continue
        continues = (
            not in_code
            and joined
            and joined[-1].strip()
            and not joined[-1].lstrip().startswith(FENCE)
            and line.strip()
            and not BLOCK_START.match(line)
            and not joined[-1].startswith("#")
        )
        if continues:
            joined[-1] = f"{joined[-1]} {line.strip()}"
        else:
            joined.append(line)
    return joined


def notes(text: str, version: str) -> str:
    body = "\n".join(join_wrapped(section(text, version))).strip()
    if not body:
        raise NotesError(f"changelog section for version {version} is empty")
    return f"{body}\n\nInstall: `uv tool install open-agent-router=={version}`\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        text = notes(args.changelog.read_text(encoding="utf-8"), args.version)
    except NotesError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
