"""Fake harness for tests of the runner and dispatcher.

Usage: python fake_harness.py --scenario <name> [scenario args]. Scenarios:
echo-file <path>, exit <code>, sleep <s>, child-sleep <s> <pid-file>, write <file>, commit,
big <MB>, final-then-hang <string>, stdin-echo, argv-echo, wait-stdin-eof, env <name>.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def out(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:
    if "--scenario" not in argv:
        out("usage: fake_harness.py --scenario <name> [args]\n")
        return 2
    at = argv.index("--scenario")
    name = argv[at + 1]
    args = argv[at + 2 :]
    if name == "echo-file":
        out(Path(args[0]).read_text(encoding="utf-8"))
        sys.stderr.write("stderr line\n")
        return 0
    if name == "exit":
        out("exiting\n")
        return int(args[0])
    if name == "sleep":
        out("sleeping\n")
        time.sleep(float(args[0]))
        return 0
    if name == "child-sleep":
        child = subprocess.Popen(
            [sys.executable, __file__, "--scenario", "sleep", args[0]], stdin=subprocess.DEVNULL
        )
        Path(args[1]).write_text(str(child.pid), encoding="utf-8")
        out("child started\n")
        child.wait()
        return 0
    if name == "write":
        Path(args[0]).write_text("written by fake harness\n", encoding="utf-8")
        out("written\n")
        return 0
    if name == "commit":
        Path("committed.txt").write_text("committed by fake harness\n", encoding="utf-8")
        for command in (["git", "add", "committed.txt"], ["git", "commit", "-q", "-m", "fake"]):
            subprocess.run(command, check=True, stdin=subprocess.DEVNULL)
        out("committed\n")
        return 0
    if name == "big":
        block = b"x" * (2**20 - 1) + b"\n"
        for _ in range(int(args[0])):
            sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
        return 0
    if name == "final-then-hang":
        out("working\n" + args[0] + "\n")
        time.sleep(600)
        return 0
    if name == "stdin-echo":
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    if name == "argv-echo":
        out(json.dumps(args) + "\n")
        return 0
    if name == "wait-stdin-eof":
        sys.stdin.buffer.read()
        out("eof\n")
        return 0
    if name == "env":
        out(os.environ.get(args[0], "") + "\n")
        return 0
    out(f"unknown scenario: {name}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
