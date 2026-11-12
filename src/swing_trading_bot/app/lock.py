"""Per-ledger, OS-backed process locks (ARCHITECTURE §10.1 / §12.20).

Each ledger file ``X`` has a lock file ``X.lock`` next to it. Ownership is decided by
``fcntl.flock`` on that file, never by its contents:

* operational commands take an **exclusive** lock; inspection and backup take a
  **shared** lock, so several readers may run together but never alongside a writer;
* acquisition never waits (``LOCK_NB``): a held lock fails fast with
  :class:`LockHeldError`;
* the lock is taken **before** the database is opened or migrated, and released only
  when the command has finished with the ledger;
* the kernel releases a flock when the holding process exits for any reason (including
  ``kill -9`` / ``os._exit``), so a crash can never leave a stale lock.

An exclusive holder writes a small JSON note (pid, command, time) into the file. It is
diagnostic text for operators only and is never used to decide ownership.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self


class LockMode(StrEnum):
    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class LockHeldError(RuntimeError):
    """Another process holds a conflicting lock on this ledger."""


def lock_path_for(ledger: Path) -> Path:
    return ledger.with_name(ledger.name + ".lock")


class LedgerLock:
    def __init__(self, ledger: Path, mode: LockMode, *, command: str) -> None:
        self.ledger = ledger
        self.path = lock_path_for(ledger)
        self.mode = mode
        self.command = command
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> Self:
        if self._fd is not None:
            raise RuntimeError("lock already held by this object")
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"directory does not exist: {self.path.parent}")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        flag = fcntl.LOCK_EX if self.mode is LockMode.EXCLUSIVE else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, flag | fcntl.LOCK_NB)
        except BlockingIOError:
            note = _read_note(fd)
            os.close(fd)
            raise LockHeldError(
                f"ledger {self.ledger} is in use by another process"
                + (f" (last exclusive holder note: {note})" if note else "")
            ) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        if self.mode is LockMode.EXCLUSIVE:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "command": self.command,
                    "acquired_at": datetime.now(UTC).isoformat(),
                    "note": "diagnostic only; ownership is the OS flock",
                },
                sort_keys=True,
            ).encode()
            os.ftruncate(fd, 0)
            os.pwrite(fd, payload, 0)
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _read_note(fd: int) -> str:
    with contextlib.suppress(OSError):
        return os.pread(fd, 512, 0).decode("utf-8", "replace").strip()
    return ""
