"""Reconciliation test environment: OMS env + an INIT broker baseline from SimBroker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from swing_trading_bot.broker.activity_feed import DEFAULT_RESCAN_POLICY
from swing_trading_bot.broker.sim import SimConfig
from swing_trading_bot.ledger.baselines import BrokerBaselinesRepository
from swing_trading_bot.oms.order_manager import CrashHook
from swing_trading_bot.oms.reconciler import Reconciler, ReconciliationReport, ReconConfig
from tests.support.oms import OMS_CONFIG, OmsEnv, oms_env
from tests.support.portfolio import NOW
from tests.support.pretrade import LIMITS

RECON_CONFIG = ReconConfig(
    oms=OMS_CONFIG,
    rescan=DEFAULT_RESCAN_POLICY,
    max_snapshot_retries=LIMITS.reconciliation.max_snapshot_retries,
    max_consecutive_inconclusive=LIMITS.reconciliation.max_consecutive_inconclusive,
)


@dataclass
class ReconEnv:
    o: OmsEnv
    crash: CrashHook | None = None

    def reconciler(self, crash: CrashHook | None = None) -> Reconciler:
        e = self.o.r.env
        return Reconciler(e.db, e.pf, self.o.read or e.sim, RECON_CONFIG, crash=crash)

    def run(self, after: timedelta | None = None) -> ReconciliationReport:
        if after is not None:
            self.o.clock.advance(after)
        return self.reconciler(self.crash).run(now=self.o.now())


def baseline(o: OmsEnv) -> None:
    acct = o.sim.get_account()
    cfg = o.sim.get_account_config()
    with o.db.transaction() as tx:
        BrokerBaselinesRepository().record_initial(
            tx,
            portfolio_id=1,
            taken_at=NOW,
            cash=acct.cash,
            equity=acct.cash,
            account_config={"no_shorting": cfg.no_shorting,
                            "max_margin_multiplier": str(cfg.max_margin_multiplier)},
        )  # fmt: skip


def recon_env(tmp_path: Path, *, sim_config: SimConfig | None = None) -> ReconEnv:
    o = oms_env(tmp_path, sim_config=sim_config)
    baseline(o)
    return ReconEnv(o)
