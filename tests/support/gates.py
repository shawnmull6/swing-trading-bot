"""A complete, healthy simulate-mode environment for gate tests (isolated temp DB)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from swing_trading_bot.domain.enums import ReconciliationResult, RuntimeMode
from swing_trading_bot.ledger.activity_coverage import advance_coverage
from swing_trading_bot.ledger.codec import ts_to_db
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    ReconciliationRunsRepository,
    ResumeOutcome,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import (
    BROKER_ACCOUNT_ID,
    BROKER_ENVIRONMENT,
    LAST_SUCCESSFUL_SYNC,
)
from swing_trading_bot.risk.gates import GateConfig, TradingGates
from tests.support.calendar_fixture import settlement_fetch, trading_fetch
from tests.support.loss_limits import CONFIG as LOSS_CONFIG
from tests.support.portfolio import NOW, new_ledger, seed_calendar

GATE_CONFIG = GateConfig(
    mode=RuntimeMode.SIMULATE,
    trading_enabled=True,
    max_sync_age=timedelta(seconds=120),
    calendar_max_age=timedelta(hours=36),
    calendar_coverage_days=10,
    calendar_horizon_warning_days=365,
    activity_posting_lag=timedelta(minutes=15),
    reconciliation_max_age=timedelta(minutes=10),
)


@dataclass
class Env:
    db: Database
    pf: VirtualPortfolio
    monitor: LossLimitMonitor
    sim: SimBroker
    clock: SimClock
    gates: TradingGates

    def refresh(
        self,
        at: datetime,
        *,
        recon: ReconciliationResult | None = ReconciliationResult.OK,
        calendar: bool = True,
        sync: bool = True,
        coverage: bool = True,
    ) -> None:
        """Simulate what a healthy sync cycle would have stored by ``at``."""
        self.clock.advance_to(at)
        if calendar:
            seed_calendar(self.db, at - timedelta(minutes=1))
        with self.db.transaction() as tx:
            sync_repo = SyncStateRepository()
            if sync:
                sync_repo.set(tx, LAST_SUCCESSFUL_SYNC, ts_to_db(at - timedelta(seconds=10)), at)
            if coverage:
                advance_coverage(
                    tx, after=None, until=at - timedelta(minutes=16), complete=True, at=at
                )
            if recon is not None:
                ReconciliationRunsRepository().append(tx, recon, None, at - timedelta(seconds=30))


def record_identity(
    db: Database, account_id: str = "sim-account-0001", environment: str = "sim"
) -> None:
    with db.transaction() as tx:
        repo = SyncStateRepository()
        repo.set(tx, BROKER_ACCOUNT_ID, account_id, NOW)
        repo.set(tx, BROKER_ENVIRONMENT, environment, NOW)


def resume(db: Database, at: datetime) -> ResumeOutcome:
    with db.transaction() as tx:
        run = ReconciliationRunsRepository().latest(tx)
        assert run is not None
        return TradingStateRepository().resume(
            tx, detail="test resume", actor="operator", at=at, reconciliation_run_id=run.id
        )


def healthy_env(
    tmp_path: Path,
    *,
    sim_config: SimConfig | None = None,
    config: GateConfig = GATE_CONFIG,
    identity: bool = True,
    resume_halt: bool = True,
    reconcile: bool = True,
) -> Env:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db, pf = new_ledger(tmp_path / "gates.sqlite3")
    clock = SimClock(NOW)
    sim = SimBroker(clock, sim_config or SimConfig())
    sim.load_calendar(trading_fetch(NOW))
    sim.load_calendar(settlement_fetch(NOW))
    monitor = LossLimitMonitor(db, pf, LOSS_CONFIG)
    env = Env(db, pf, monitor, sim, clock, TradingGates(db, pf, monitor, sim, config))
    if identity:
        record_identity(db)
    env.refresh(NOW, recon=ReconciliationResult.OK if reconcile else None)
    monitor.establish_baselines(now=NOW)
    if resume_halt and reconcile:
        assert resume(db, NOW) is ResumeOutcome.RESUMED  # clears the INITIALIZED halt
    return env
