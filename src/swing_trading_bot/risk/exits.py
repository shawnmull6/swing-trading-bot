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
  broker order that could already be selling or buying those shares;
* no known broker restriction that is documented to prevent *every* order write
  (``trade_suspended_by_user`` or ``suspend_trade`` true → ``BROKER_ORDERS_SUSPENDED``;
  M2-1, A§12.34).

Account restrictions that only stop new entries (``closing_transactions_only``,
``transfers_blocked``) are reported as entry-only restrictions. An unknown restriction
field, or an unreadable account configuration, is reported in
``broker_eligibility_unverified`` and never turned into a failure: it neither makes an
exit unsafe nor is it evidence that the broker would accept one.

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
from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot
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
from swing_trading_bot.risk.account_restrictions import RestrictionField, assess_restrictions
from swing_trading_bot.risk.gates import EXPECTED_ENVIRONMENT

_SUSPENSION_FIELDS = frozenset(
    {RestrictionField.TRADE_SUSPENDED_BY_USER, RestrictionField.SUSPEND_TRADE}
)


class ExitFailure(StrEnum):
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    BROKER_AUTH_FAILED = "BROKER_AUTH_FAILED"
    BROKER_DATA_INVALID = "BROKER_DATA_INVALID"
    BROKER_IDENTITY_UNVERIFIED = "BROKER_IDENTITY_UNVERIFIED"
    BROKER_IDENTITY_MISMATCH = "BROKER_IDENTITY_MISMATCH"
    BROKER_ENVIRONMENT_MISMATCH = "BROKER_ENVIRONMENT_MISMATCH"
    BROKER_ACCOUNT_RESTRICTED = "BROKER_ACCOUNT_RESTRICTED"
    BROKER_ORDERS_SUSPENDED = "BROKER_ORDERS_SUSPENDED"
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
    broker_eligibility_unverified: tuple[str, ...] = ()
    """Why the broker's acceptance of an exit order is not established (unknown
    restriction fields, unreadable configuration, unconfirmed field semantics). Reported
    only; never a failure and never a permission (M2-1)."""
    execution_available: bool = False  # exit execution is not implemented in M1

    @property
    def safe_to_attempt(self) -> bool:
        return not self.failures

    @property
    def broker_eligibility_confirmed(self) -> bool:
        """True only when the exit is safe to attempt **and** every restriction field was
        reported, so nothing about the broker's acceptance is unknown. ``safe_to_attempt``
        alone is never evidence of broker permission (M2-1)."""
        return self.safe_to_attempt and not self.broker_eligibility_unverified

    @property
    def confirmed_qty(self) -> Decimal | None:
        """The quantity both sides agree on, only when the assessment is safe."""
        return self.ledger_qty if self.safe_to_attempt else None


@dataclass(slots=True)
class _BrokerEvidence:
    account: AccountSnapshot | None = None
    config: AccountConfigSnapshot | None = None
    config_problem: str | None = None
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
        entry_only, unverified = self._restrictions(evidence, failures)
        return ExitReadiness(
            symbol=symbol,
            evaluated_at=now,
            failures=tuple(sorted(set(failures))),
            entry_only_restrictions=tuple(sorted({*restrictions, *entry_only})),
            ledger_qty=ledger_qty,
            broker_qty=broker_qty,
            broker_eligibility_unverified=unverified,
        )

    # -------------------------------------------------------------- broker
    def _read_broker(self, failures: list[ExitFinding]) -> _BrokerEvidence:
        assert_no_transaction("broker reads for exit readiness")
        ev = _BrokerEvidence()
        ev.account = _call(failures, "account", self._broker.get_account)
        config_failures: list[ExitFinding] = []
        ev.config = _call(config_failures, "account configuration", self._broker.get_account_config)
        if config_failures:  # eligibility evidence only: reported, never an exit failure
            ev.config_problem = f"{config_failures[0].code}: {config_failures[0].detail}"
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

    # ---------------------------------------------------------- restrictions
    @staticmethod
    def _restrictions(
        ev: _BrokerEvidence, failures: list[ExitFinding]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """M2-1: (entry-only restriction codes, unverified-eligibility notes). Only a
        known restriction documented to prevent every order write becomes a failure."""
        entry_only: list[str] = []
        unverified: list[str] = []
        if ev.account is None:
            unverified.append("account unavailable: restriction state unknown")
        if ev.config is None:
            unverified.append(
                "account configuration unavailable: suspend_trade / "
                "closing_transactions_only unknown"
                + (f" ({ev.config_problem})" if ev.config_problem else "")
            )
        r = assess_restrictions(ev.account, ev.config)
        # status / trading_blocked / account_blocked stay BROKER_ACCOUNT_RESTRICTED.
        suspended = [f"{f.value}=true" for f in r.active_fields if f in _SUSPENSION_FIELDS]
        if suspended:
            failures.append(ExitFinding(ExitFailure.BROKER_ORDERS_SUSPENDED, ", ".join(suspended)))
        if RestrictionField.CLOSING_TRANSACTIONS_ONLY in r.active_fields:
            entry_only.append("BROKER_CLOSING_TRANSACTIONS_ONLY")
            unverified.append("closing_transactions_only=true: semantics not confirmed")
        if RestrictionField.TRANSFERS_BLOCKED in r.active_fields:
            entry_only.append("BROKER_TRANSFERS_BLOCKED")
        unverified += [f"{f.value} not reported" for f in r.unreported]
        return tuple(entry_only), tuple(unverified)

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
