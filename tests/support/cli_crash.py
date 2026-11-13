"""Child process for process-level CLI crash tests: runs the real CLI against an existing
ledger with the deterministic fake account and dies with ``os._exit`` at a crash point."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tests.support.alpaca_account import Clock, FakeAlpacaAccount
from tests.support.cli_env import CliEnv

CRASH_EXIT = 70


def run_and_die(root: str, now_iso: str, command: str, point: str) -> None:
    def die(reached: str) -> None:
        if reached == point:
            os._exit(CRASH_EXIT)

    account = FakeAlpacaAccount(clock=Clock(datetime.fromisoformat(now_iso)))
    env = CliEnv(Path(root), account=account, crash=die)
    env.run(command)
    os._exit(0)  # the crash point was not reached


def run_during_outage(root: str, now_iso: str, command: str) -> None:
    """A separate process running ``command`` while every broker request fails (503)."""
    from swing_trading_bot.broker.errors import ServiceUnavailableError  # noqa: PLC0415

    account = FakeAlpacaAccount(clock=Clock(datetime.fromisoformat(now_iso)))
    account.down["*"] = lambda: ServiceUnavailableError("HTTP 503", status_code=503)
    code, _ = CliEnv(Path(root), account=account).run(command)
    os._exit(code)


def run_paused(root: str, now_iso: str, command: str, point: str, barrier: object) -> None:
    """Run ``command``; at ``point`` (inside the locked section) wait 1.5 s so concurrent
    starters overlap. Exits with the command's exit code. ``barrier`` starts all together."""
    import time  # noqa: PLC0415

    def pause(reached: str) -> None:
        if reached == point:
            time.sleep(1.5)

    barrier.wait(30)  # type: ignore[attr-defined]
    account = FakeAlpacaAccount(clock=Clock(datetime.fromisoformat(now_iso)))
    code, _ = CliEnv(Path(root), account=account, crash=pause).run(command)
    os._exit(code)


def backup_and_die(ledger: str, target: str) -> None:
    """Start a backup and die after writing part of the copy (kill mid-backup)."""
    from swing_trading_bot.ledger import backup as backup_module  # noqa: PLC0415

    def half_copy(_source: object, destination: Path) -> None:
        destination.write_bytes(Path(ledger).read_bytes()[:8192])
        os._exit(CRASH_EXIT)

    backup_module._copy = half_copy  # type: ignore[assignment]
    backup_module.backup_database(Path(ledger), Path(target))
