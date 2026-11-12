"""Read-only exit-readiness assessment (step 10 refinement; ARCHITECTURE §7, §12.13).

An exit (selling a held long position) reduces risk, so restrictions that exist to stop
*new* risk — a persistent halt, a daily lockout, a weekly drawdown limit, the trading
switch — are reported as ``entry_only_restrictions`` and never make an exit unsafe.

An exit is *safe to attempt* only when the evidence for it is solid:

* the broker is reachable and returns valid data (account, positions, open orders);
* it is the recorded account, in the environment the ledger expects, and not restricted;
* the ledger passes integrity and invariant checks;
* the holding is **confirmed** — the ledger and the broker both show a long position and
  the quantities agree (missing evidence on either side is never treated as safe);
* there is **no conflicting order state** for the symbol — no unresolved local order
  (any non-terminal status, including ambiguous submissions and orphans) and no open
  broker order that could already be selling or buying those shares.

This module only assesses. Exit execution does not exist in M1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
)
from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.domain.enums import OrderStatus, RuntimeMode
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.db import Database, Session, assert_no_transaction
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio, market_date
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    PositionsRepository,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID, BROKER_ENVIRONMENT
from swing_trading_bot.risk.gates import EXPECTED_ENVIRONMENT


class ExitFailure(StrEnum):
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    BROKER_AUTH_FAILED = "BROKER_AUTH_FAILED"
    BROKER_DATA_INVALID = "BROKER_DATA_INVALID"
    BROKER_IDENTITY_UNVERIFIED = "BROKER_IDENTITY_UNVERIFIED"
    BROKER_IDENTITY_MISMATCH = "BROKER_IDENTITY_MISMATCH"
    BROKER_ENVIRONMENT_MISMATCH = "BROKER_ENVIRONMENT_MISMATCH"
    BROKER_ACCOUNT_RESTRICTED = "BROKER_ACCOUNT_RESTRICTED"
    LEDGER_INTEGRITY_FAILED = "LEDGER_INTEGRITY_FAILED"
    LEDGER_INVARIANT_VIOLATED = "LEDGER_INVARIANT_VIOLATED"
    PORTFOLIO_MISSING = "PORTFOLIO_MISSING"
    NO_LEDGER_POSITION = "NO_LEDGER_POSITION"
    NO_BROKER_POSITION = "NO_BROKER_POSITION"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    UNRESOLVED_LOCAL_ORDERS = "UNRESOLVED_LOCAL_ORDERS"
    BROKER_OPEN_ORDERS = "BROKER_OPEN_ORDERS"


@dataclass(frozen=True, slots=True, order=True)
class ExitFinding:
    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExitReadiness:
    symbol: str
    evaluated_at: datetime
    failures: tuple[ExitFinding, ...]
    entry_only_restrictions: tuple[str, ...]
    ledger_qty: Decimal | None
    broker_qty: Decimal | None
    execution_available: bool = False  # exit execution is not implemented in M1

    @property
    def safe_to_attempt(self) -> bool:
        return not self.failures

    @property
    def confirmed_qty(self) -> Decimal | None:
        """The quantity both sides agree on, only when the assessment is safe."""
        return self.ledger_qty if self.safe_to_attempt else None


@dataclass(slots=True)
class _BrokerEvidence:
    account: AccountSnapshot | None = None
    positions: list[BrokerPosition] | None = None
    open_orders: list[BrokerOrderSnapshot] | None = None


class ExitReadinessAssessor:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        loss_limits: LossLimitMonitor,
        broker: BrokerReadPort,
        *,
        mode: RuntimeMode,
        trading_enabled: bool,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._loss = loss_limits
        self._broker = broker
        self._mode = mode
        self._trading_enabled = trading_enabled

    def assess(self, symbol: str, *, now: datetime) -> ExitReadiness:
        failures: list[ExitFinding] = []
        evidence = self._read_broker(failures)
        with self._db.read() as s:
            restrictions = self._entry_only(s, now)
            ledger_qty = self._ledger(s, symbol, now, evidence, failures)
        broker_qty = self._holding(symbol, ledger_qty, evidence, failures)
        self._orders(symbol, evidence, failures)
        return ExitReadiness(
            symbol=symbol,
            evaluated_at=now,
            failures=tuple(sorted(set(failures))),
            entry_only_restrictions=restrictions,
            ledger_qty=ledger_qty,
            broker_qty=broker_qty,
        )

    # -------------------------------------------------------------- broker
    def _read_broker(self, failures: list[ExitFinding]) -> _BrokerEvidence:
        assert_no_transaction("broker reads for exit readiness")
        ev = _BrokerEvidence()
        ev.account = _call(failures, "account", self._broker.get_account)
        ev.positions = _call(failures, "positions", self._broker.get_positions)
        ev.open_orders = _call(failures, "open orders", self._broker.get_open_orders)
        return ev

    # -------------------------------------------------------------- ledger
    def _entry_only(self, s: Session, now: datetime) -> tuple[str, ...]:
        out: list[str] = []
        state = TradingStateRepository().get(s)
        if state.halted:
            out.append(f"PERSISTENT_HALT:{state.reason_code}")
        if not self._trading_enabled:
            out.append("TRADING_DISABLED")
        try:
            report = self._loss.report(s, now=now)
            out.extend(b.value for b in report.blockers)
        except PersistenceError:
            pass  # PORTFOLIO_MISSING is reported as a fundamental failure
        return tuple(sorted(set(out)))

    def _ledger(
        self,
        s: Session,
        symbol: str,
        now: datetime,
        ev: _BrokerEvidence,
        failures: list[ExitFinding],
    ) -> Decimal | None:
        report = integrity_report(s)
        if not report.ok:
            failures.append(ExitFinding(ExitFailure.LEDGER_INTEGRITY_FAILED,
                                        "; ".join(report.problems())))  # fmt: skip
        try:
            portfolio = self._pf.portfolio(s)
        except PersistenceError:
            failures.append(ExitFinding(ExitFailure.PORTFOLIO_MISSING))
            return None
        today = market_date(now)
        invariants = accounting_checker(lambda: today).run(s)
        if not invariants.ok:
            names = sorted({v.invariant for v in invariants.violations})
            failures.append(ExitFinding(ExitFailure.LEDGER_INVARIANT_VIOLATED, ", ".join(names)))
        self._identity(s, ev, failures)
        position = PositionsRepository().get(s, portfolio.id or 0, symbol)
        orders = OrdersRepository()
        local = [
            o
            for o in (*orders.list_working(s), *orders.list_by_status(s, OrderStatus.ORPHAN))
            if o.symbol == symbol
        ]
        if local:
            failures.append(ExitFinding(ExitFailure.UNRESOLVED_LOCAL_ORDERS, ", ".join(
                sorted(f"{o.client_order_id}={o.status.value}" for o in local))))  # fmt: skip
        if position is None or position.qty <= ZERO:
            failures.append(ExitFinding(ExitFailure.NO_LEDGER_POSITION,
                                        "ledger shows no long position"))  # fmt: skip
            return None if position is None else position.qty
        return position.qty

    def _identity(self, s: Session, ev: _BrokerEvidence, failures: list[ExitFinding]) -> None:
        sync = SyncStateRepository()
        recorded_id = sync.get(s, BROKER_ACCOUNT_ID)
        recorded_env = sync.get(s, BROKER_ENVIRONMENT)
        expected_env = EXPECTED_ENVIRONMENT[self._mode]
        if recorded_id is None or recorded_env is None:
            failures.append(ExitFinding(ExitFailure.BROKER_IDENTITY_UNVERIFIED))
        elif recorded_env != expected_env:
            failures.append(ExitFinding(ExitFailure.BROKER_ENVIRONMENT_MISMATCH, recorded_env))
        account = ev.account
        if account is None:
            return
        if account.environment != expected_env:
            failures.append(ExitFinding(ExitFailure.BROKER_ENVIRONMENT_MISMATCH,
                                        account.environment))  # fmt: skip
        if recorded_id is not None and account.account_id != recorded_id:
            failures.append(ExitFinding(ExitFailure.BROKER_IDENTITY_MISMATCH))
        if account.status != "ACTIVE" or account.trading_blocked or account.account_blocked:
            failures.append(ExitFinding(ExitFailure.BROKER_ACCOUNT_RESTRICTED, account.status))

    # ----------------------------------------------------- holdings & orders
    @staticmethod
    def _holding(
        symbol: str, ledger_qty: Decimal | None, ev: _BrokerEvidence, failures: list[ExitFinding]
    ) -> Decimal | None:
        if ev.positions is None:
            return None  # the read failure is already a failure
        matches = [p for p in ev.positions if p.symbol == symbol]
        broker_qty = matches[0].qty if matches else None
        if broker_qty is None or broker_qty <= ZERO:
            failures.append(ExitFinding(ExitFailure.NO_BROKER_POSITION,
                                        "broker shows no long position"))  # fmt: skip
        elif ledger_qty is not None and ledger_qty > ZERO and broker_qty != ledger_qty:
            failures.append(ExitFinding(ExitFailure.POSITION_MISMATCH,
                                        f"ledger {ledger_qty} vs broker {broker_qty}"))  # fmt: skip
        return broker_qty

    @staticmethod
    def _orders(symbol: str, ev: _BrokerEvidence, failures: list[ExitFinding]) -> None:
        if ev.open_orders is None:
            return
        conflicting = sorted(o.client_order_id for o in ev.open_orders if o.symbol == symbol)
        if conflicting:
            failures.append(ExitFinding(ExitFailure.BROKER_OPEN_ORDERS, ", ".join(conflicting)))


def _call[T](failures: list[ExitFinding], what: str, fn: Callable[[], T]) -> T | None:
    try:
        return fn()
    except AuthenticationError:
        failures.append(ExitFinding(ExitFailure.BROKER_AUTH_FAILED, what))
    except (InvalidBrokerDataError, IncompleteRetrievalError) as exc:
        failures.append(ExitFinding(ExitFailure.BROKER_DATA_INVALID, f"{what}: {exc}"))
    except BrokerError as exc:
        failures.append(ExitFinding(ExitFailure.BROKER_UNREACHABLE,
                                    f"{what}: {type(exc).__name__}"))  # fmt: skip
    return None
