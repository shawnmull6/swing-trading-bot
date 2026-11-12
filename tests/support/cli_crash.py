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
