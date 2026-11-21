"""One serve process per gateway home: an OS lock on ``home / "serve.lock"``.

The lock file itself is never removed — only the OS-level lock matters, so a file left
behind by a dead process does not block the next start.
"""

from __future__ import annotations

import errno
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


_LOCKED_ERRNOS = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
)


class InstanceLocked(RuntimeError):
    """Another process already serves this gateway home."""

    def __init__(self, home: Path) -> None:
        self.home = home
        super().__init__(f"gateway already running for {home}")


@contextmanager
def instance_lock(home: Path) -> Iterator[None]:
    """Hold the OS lock of ``home / "serve.lock"``; raise InstanceLocked if it is taken."""
    stream = (home / "serve.lock").open("a+b")
    try:
        _acquire(stream, home)
        try:
            yield
        finally:
            _release(stream)
    finally:
        stream.close()


def _acquire(stream, home: Path) -> None:
    try:
        if sys.platform == "win32":
            # the locked byte range starts at the current position
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # contention: EACCES/EDEADLOCK from msvcrt, EAGAIN/EWOULDBLOCK from flock;
        # any other error is a real I/O problem and goes up as is
        if exc.errno in _LOCKED_ERRNOS:
            raise InstanceLocked(home) from exc
        raise


def _release(stream) -> None:
    if sys.platform == "win32":
        # unlock needs the exact range of the matching lock
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
