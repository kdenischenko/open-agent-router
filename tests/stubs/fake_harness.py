"""Fake harness for tests of the runner and dispatcher.

Usage: python fake_harness.py --scenario <name> [scenario args]. Scenarios:
echo-file <path>, exit <code>, sleep <s>, quiet-sleep <s>, quiet-busy <s>,
tick <count> <interval_s> [stdout|stderr],
retry <count> <interval_s>, child-sleep <s> <pid-file>, detached-child <s> <pid-file>,
orphan-sleep <s> <child-s> <pid-file>,
write <file>, work-silently <file> <seconds> [interval_s], commit, big <MB>,
final-then-hang <string>, stdin-echo, argv-echo, wait-stdin-eof, env <name>.
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
    if name == "quiet-sleep":
        # says nothing at all: only the extra activity signals can tell it is alive
        time.sleep(float(args[0]))
        return 0
    if name == "quiet-busy":
        # says nothing but burns CPU: the CPU signal is the only sign of life
        end = time.monotonic() + float(args[0])
        total = 0
        while time.monotonic() < end:
            total += sum(i * i for i in range(2000))
        return 0
    if name == "tick":
        count = int(args[0])
        interval = float(args[1])
        to_stderr = len(args) > 2 and args[2] == "stderr"
        for number in range(1, count + 1):
            if to_stderr:
                sys.stderr.write(f"tick {number}\n")
                sys.stderr.flush()
            else:
                out(f"tick {number}\n")
            if number < count:
                time.sleep(interval)
        out("ok:done\n")
        return 0
    if name == "retry":
        for number in range(int(args[0])):
            out(json.dumps({"type": "retry", "attempt": number + 1}) + "\n")
            time.sleep(float(args[1]))
        time.sleep(120)
        return 0
    if name == "child-sleep":
        child = subprocess.Popen(
            [sys.executable, __file__, "--scenario", "sleep", args[0]], stdin=subprocess.DEVNULL
        )
        Path(args[1]).write_text(str(child.pid), encoding="utf-8")
        out("child started\n")
        child.wait()
        return 0
    if name == "detached-child":
        # starts a sleeping grandchild and exits at once: the grandchild outlives its parent
        child = subprocess.Popen(
            [sys.executable, __file__, "--scenario", "sleep", args[0]], stdin=subprocess.DEVNULL
        )
        Path(args[1]).write_text(str(child.pid), encoding="utf-8")
        return 0
    if name == "orphan-sleep":
        # a grandchild that outlives the harness: only a job object reaches it
        middle = subprocess.Popen(
            [sys.executable, __file__, "--scenario", "detached-child", args[1], args[2]],
            stdin=subprocess.DEVNULL,
        )
        out("grandchild starting\n")
        middle.wait()
        time.sleep(float(args[0]))
        return 0
    if name == "write":
        Path(args[0]).write_text("written by fake harness\n", encoding="utf-8")
        out("written\n")
        return 0
    if name == "work-silently":
        # works without saying anything: keeps writing a file in its working directory
        target = Path(args[0])
        deadline = time.monotonic() + float(args[1])
        number = 0
        while time.monotonic() < deadline:
            number += 1
            target.write_text(f"work {number}\n", encoding="utf-8")
            time.sleep(float(args[2]) if len(args) > 2 else 0.05)
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
