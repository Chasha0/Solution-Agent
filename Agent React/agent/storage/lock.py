"""Per-session file lock (msvcrt on Windows, fcntl on Unix).

A single ``<session>/.lock`` file is used for all writes to that session.
Non-blocking by default; retries every 50 ms up to ``timeout_s``. See spec
§4.3 for the storage layout that references this lock.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import IO

_IS_WINDOWS = sys.platform == "win32"


class LockTimeout(Exception):
    """Raised when FileLock acquisition times out."""


class FileLock:
    """Exclusive file lock context manager (cross-platform).

    Usage::

        with FileLock(path):
            ...  # critical section

    On Windows uses :func:`msvcrt.locking` in non-blocking mode (``LK_NLCK``)
    on a single byte at offset 0. On Unix uses :func:`fcntl.flock` with
    ``LOCK_EX | LOCK_NB``.
    """

    def __init__(self, path: Path, timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout_s = float(timeout_s)
        self._fp: IO[bytes] | None = None
        self._fd: int | None = None

    # ---- context manager protocol ----

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure the lock file exists; msvcrt/fcntl require a real file.
        self.path.touch(exist_ok=True)

        deadline = time.monotonic() + self.timeout_s
        last_err: Exception | None = None
        while True:
            try:
                # Python's built-in open() on Windows gives us a C-runtime fd
                # that msvcrt.locking understands. os.open() would give a raw
                # Win32 handle which is NOT compatible.
                self._fp = open(self.path, "r+b")
                self._fd = self._fp.fileno()
                if _IS_WINDOWS:
                    self._lock_windows(self._fd)
                else:
                    self._lock_unix(self._fd)
                return self
            except (OSError, PermissionError) as e:
                self._close_fp_quietly()
                last_err = e
                if time.monotonic() >= deadline:
                    break
                # Short sleep; gives other holders a chance to release.
                time.sleep(0.05)

        raise LockTimeout(
            f"Could not acquire lock on {self.path} within {self.timeout_s}s "
            f"(last error: {last_err!r})"
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            self._close_fp_quietly()
            return
        try:
            if _IS_WINDOWS:
                self._unlock_windows(self._fd)
            else:
                self._unlock_unix(self._fd)
        except OSError:
            # Unlock failure (e.g. process killed mid-lock) is non-fatal;
            # the OS releases the lock when the fd closes anyway.
            pass
        self._close_fp_quietly()

    # ---- internals ----

    def _close_fp_quietly(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
        self._fp = None
        self._fd = None

    @staticmethod
    def _lock_windows(fd: int) -> None:
        import msvcrt

        # msvcrt constants: LK_LOCK=0, LK_RLCK=1, LK_NBLCK=2, LK_NBRLCK=3, LK_UNLCK=4.
        # LK_NBLCK = non-blocking exclusive lock — raises PermissionError (errno 13)
        # or OSError (errno 36, sharing violation) if another holder exists.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _unlock_windows(fd: int) -> None:
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _lock_unix(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_unix(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)