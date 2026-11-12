"""Child-process helpers for lock tests (spawned; no sockets)."""

from __future__ import annotations

import os
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Event
from pathlib import Path

from swing_trading_bot.app.lock import LedgerLock, LockMode


def hold(ledger: str, mode: str, ready: Connection, release: Event) -> None:
    """Hold a lock until told to release."""
    with LedgerLock(Path(ledger), LockMode(mode), command="holder"):
        ready.send("held")
        release.wait(60)


def die_holding(ledger: str, ready: Connection) -> None:
    """Take the exclusive lock and die without releasing it (like kill -9)."""
    LedgerLock(Path(ledger), LockMode.EXCLUSIVE, command="doomed").acquire()
    ready.send("held")
    os._exit(9)
