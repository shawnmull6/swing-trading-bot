"""Drive the real CLI (``app.cli.main``) offline: tmp data dir, repo config, injected
clock and a fake Alpaca account behind the real read adapter. No ``.env``, no network."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swing_trading_bot.app.cli import main
from swing_trading_bot.app.commands import Dependencies
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.migrator import Migration
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.oms.order_manager import CrashHook
from tests.support.alpaca_account import FakeAlpacaAccount

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
ECONOMIC = (
    "broker_activities",
    "cash_entries",
    "fills",
    "orders",
    "cash_reservations",
    "positions",
    "settlement_resolutions",
    "broker_baselines",
)


@dataclass
class CliEnv:
    root: Path
    account: FakeAlpacaAccount = field(default_factory=FakeAlpacaAccount)
    broker: bool = True
    migrations: tuple[Migration, ...] | None = None
    crash: CrashHook | None = None
    last_text: str = ""

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def ledger(self) -> Path:
        return self.data / "ledger.sqlite3"

    def deps(self) -> Dependencies:
        return Dependencies(
            clock=self.account.clock,
            broker_factory=self.account.adapter if self.broker else None,
            migrations=self.migrations,
            crash=self.crash,
        )

    def run(self, *argv: str, actor: str = "operator:test") -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        args = [*argv, "--data-dir", str(self.data), "--config-dir", str(CONFIG_DIR), "--json",
                "--actor", actor]  # fmt: skip
        code = main(args, deps=self.deps(), stdout=out)
        self.last_text = out.getvalue()
        return code, json.loads(self.last_text)

    def init(self) -> dict[str, Any]:
        code, env = self.run(
            "init-ledger",
            "--confirm-path", str(self.ledger),
            "--confirm-account", self.account.account_id,
            "--confirm-environment", "paper",
        )  # fmt: skip
        assert code == 0, env
        return env

    def ready(self) -> None:
        """init → let the first activity window close → clean reconcile → resume."""
        self.init()
        self.account.clock.advance(minutes=20)
        code, env = self.run("reconcile")
        assert code == 0, env
        code, env = self.run("resume", "--reason", "initial resume")
        assert code == 0, env

    def open(self) -> Database:
        return Database.open(self.ledger, RuntimeMode.OBSERVE, auto_migrate=False)

    def fingerprint(self, tables: tuple[str, ...] | None = None) -> str:
        with self.open() as db, db.read() as s:
            return ledger_fingerprint(s, tables) if tables else ledger_fingerprint(s)

    def scalar(self, sql: str) -> Any:
        with self.open() as db, db.read() as s:
            return s.scalar(sql)


def cli_env(tmp_path: Path, **kw: Any) -> CliEnv:
    env = CliEnv(tmp_path, **kw)
    env.data.mkdir()
    return env
