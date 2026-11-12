"""OrderManager test environment: healthy simulate-mode ledger + SimBroker + risk engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from swing_trading_bot.broker.ports import BrokerExecutionPort, BrokerReadPort
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from swing_trading_bot.domain.enums import OrderStatus, ReconciliationResult, RuntimeMode
from swing_trading_bot.ledger.balances import Balances
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.repositories import (
    OrderEventsRepository,
    OrderRecord,
    OrdersRepository,
    ReconciliationRunsRepository,
    ReservationRecord,
    ReservationsRepository,
    TradingState,
    TradingStateRepository,
)
from swing_trading_bot.oms.execution import SimulationExecution
from swing_trading_bot.oms.order_manager import (
    CrashHook,
    OmsConfig,
    OrderManager,
    PlacementResult,
)
from swing_trading_bot.oms.recovery import AmbiguousOrderRecovery
from swing_trading_bot.oms.sync import OrderSync
from swing_trading_bot.risk.pretrade import RiskDecision
from tests.support.portfolio import NOW
from tests.support.pretrade import LIMITS, RiskEnv, proposal, risk_env

OMS_CONFIG = OmsConfig(mode=RuntimeMode.SIMULATE, ambiguous=LIMITS.ambiguous_orders)
D = Decimal


class SimulatedCrash(BaseException):
    """Raised by a crash hook: unwinds like a killed process (open transactions roll
    back), and is a BaseException so no ``except Exception`` can swallow it."""


def crash_at(point: str) -> CrashHook:
    def hook(reached: str) -> None:
        if reached == point:
            raise SimulatedCrash(point)

    return hook


@dataclass
class OmsEnv:
    r: RiskEnv
    crash: CrashHook | None = None
    read: BrokerReadPort | None = None
    execution: BrokerExecutionPort | None = None
    om: OrderManager = field(init=False)
    sync: OrderSync = field(init=False)
    recovery: AmbiguousOrderRecovery = field(init=False)

    def __post_init__(self) -> None:
        self.rebuild(self.crash)

    def rebuild(self, crash: CrashHook | None = None) -> None:
        """New collaborators over the same ledger and broker: a process restart."""
        e = self.r.env
        read = self.read or e.sim
        execution = SimulationExecution(self.execution or e.sim)
        self.om = OrderManager(e.db, e.pf, self.r.engine, e.gates, read, execution, OMS_CONFIG,
                               crash=crash)  # fmt: skip
        self.sync = OrderSync(e.db, e.pf, read, OMS_CONFIG, crash=crash)
        self.recovery = AmbiguousOrderRecovery(e.db, e.pf, read, OMS_CONFIG)

    # ------------------------------------------------------------ shortcuts
    @property
    def db(self) -> Database:
        return self.r.env.db

    @property
    def sim(self) -> SimBroker:
        return self.r.env.sim

    @property
    def clock(self) -> SimClock:
        return self.r.env.clock

    def now(self) -> datetime:
        return self.clock.now()

    def decide(self, symbol: str = "MSFT", entry: str = "50.00", stop: str = "48.00",
               **kw: Any) -> RiskDecision:  # fmt: skip
        return self.r.evaluate(proposal(symbol, entry, stop, **kw), at=self.now())

    def place(self, symbol: str = "MSFT", entry: str = "50.00", stop: str = "48.00",
              **kw: Any) -> PlacementResult:  # fmt: skip
        decision = self.decide(symbol, entry, stop, **kw)
        assert decision.approved, decision.rejections
        return self.om.place_entry(decision, now=self.now())

    def advance(self, delta: timedelta, *, refresh: bool = True,
                marks: dict[str, str] | None = None) -> datetime:  # fmt: skip
        """Move simulated time; optionally store what a healthy sync cycle would have
        (calendar, sync time, coverage, a clean reconciliation run) and fresh marks."""
        at = self.clock.advance(delta)
        if refresh:
            self.r.env.refresh(at)
            self.r.set_marks(marks or {"MSFT": "50.00"}, at=at)
        return at

    def order(self, coid: str) -> OrderRecord:
        with self.db.read() as s:
            return OrdersRepository().require(s, coid)

    def reservation(self, coid: str) -> ReservationRecord:
        with self.db.read() as s:
            res = ReservationsRepository().get(s, coid)
        assert res is not None
        return res

    def events(self, coid: str) -> list[str]:
        with self.db.read() as s:
            return [str((e.detail or {}).get("event"))
                    for e in OrderEventsRepository().list_for_order(s, coid)]  # fmt: skip

    def state(self) -> TradingState:
        with self.db.read() as s:
            return TradingStateRepository().get(s)

    def submits(self, coid: str | None = None) -> list[str]:
        """Submit requests the broker saw (one line per request)."""
        return [line for line in self.sim.event_log
                if " SUBMIT " in line and "response lost" not in line
                and (coid is None or coid in line)]  # fmt: skip

    def clean_reconciliation(self, at: datetime | None = None) -> None:
        with self.db.transaction() as tx:
            ReconciliationRunsRepository().append(tx, ReconciliationResult.OK, None,
                                                  at or self.now())  # fmt: skip

    def balances(self) -> Balances:
        with self.db.read() as s:
            return self.r.env.pf.balances(s, now=self.now())


def oms_env(
    tmp_path: Path,
    *,
    sim_config: SimConfig | None = None,
    crash: CrashHook | None = None,
) -> OmsEnv:
    return OmsEnv(risk_env(tmp_path, sim_config=sim_config), crash=crash)


WORKING = (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL)
__all__ = ["NOW", "OMS_CONFIG", "WORKING", "OmsEnv", "SimulatedCrash", "crash_at", "oms_env"]
