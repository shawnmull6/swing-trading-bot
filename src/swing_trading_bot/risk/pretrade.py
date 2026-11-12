"""Deterministic pre-trade risk evaluation (ARCHITECTURE §11.1, M1 step 11).

``PreTradeRiskEngine.evaluate`` turns a :class:`TradeProposal` (a long limit entry with
an initial stop) into a :class:`RiskDecision`: approved whole-share quantity or
machine-readable rejections, the sizing preview, and the evidence used. It writes
nothing and creates no orders.

Sizing — every constraint is a cap on whole shares; the approved quantity is the
minimum of all caps, found by searching downward from a closed-form estimate (never
rounded up) and verified exactly:

=====================  =============================================================
constraint             condition on quantity q (E = current equity, B = baseline)
=====================  =============================================================
RISK_BUDGET            q*(entry-stop) + buy_fee(q) + sell_fee(q, stop) <= 0.5% * E
POSITION_CONCENTRATION held_mv + pending(symbol) + q*entry <= 20% * E   (market value)
ORDER_COMMITMENT       reserve(q) = ceil(q*entry + buy_fee(q)) <= $1,000
CAPITAL_CEILING        reserve(q) <= $5,000 - (open cost basis + active buy commitments)
AVAILABLE_CASH         reserve(q) <= settled cash - active buy commitments
GAP_STRESS_POSITION    15% * (held_mv + pending(symbol) + q*entry) <= 3% * B
GAP_STRESS_PORTFOLIO   portfolio stressed loss + 15% * q*entry <= 10% * B
BROKER_CASH_BACKSTOP   q*entry + buy_fee(q) <= min(broker cash, non-marginable BP)
=====================  =============================================================

Discrete checks: long-only limit entry, whole shares, no extended hours, supported
instrument, valid tick prices, stop strictly below entry, $10 minimum (entry and fresh
mark), max 4 positions counting pending entries, no duplicate working order or intent,
fresh mark for the symbol, a fresh gate status allowing new entries, fresh broker
account evidence for the recorded account, ledger integrity/invariants, clean recent
reconciliation, loss-limit eligibility and an available baseline.

A decision records a ledger-state fingerprint and expires quickly
(:meth:`RiskDecision.still_valid`); it is never a reusable authorization (§12.14).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.config.risk_limits import RiskLimits
from swing_trading_bot.domain.calendar import CalendarError
from swing_trading_bot.domain.enums import (
    InstrumentType,
    OrderType,
    ReconciliationResult,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.models import PriceMark
from swing_trading_bot.domain.money import (
    DECIMAL_CONTEXT,
    ZERO,
    MoneyError,
    is_whole,
    reservation_amount,
    valid_order_price,
)
from swing_trading_bot.domain.valuation import MarkFreshnessPolicy, assess_mark
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database, Session
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.loss_limits import LossBlocker, LossLimitMonitor, LossLimitReport
from swing_trading_bot.ledger.portfolio import PortfolioSnapshot, VirtualPortfolio, market_date
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    PriceMarksRepository,
    ReconciliationRunsRepository,
    ReservationsRepository,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID
from swing_trading_bot.risk.gates import EXPECTED_ENVIRONMENT, GateStatus

_SYMBOL_OK: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.")


# ------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """A candidate long entry. ``requested_qty`` None asks for the maximum permitted."""

    intent_key: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    entry_price: Decimal
    stop_price: Decimal
    instrument_type: InstrumentType
    extended_hours: bool = False
    requested_qty: Decimal | None = None


class RejectionCode(StrEnum):
    # proposal shape
    NOT_LONG_ENTRY = "NOT_LONG_ENTRY"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    UNSUPPORTED_TIME_IN_FORCE = "UNSUPPORTED_TIME_IN_FORCE"
    EXTENDED_HOURS_NOT_ALLOWED = "EXTENDED_HOURS_NOT_ALLOWED"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    INVALID_INTENT_KEY = "INVALID_INTENT_KEY"
    INVALID_REQUESTED_QUANTITY = "INVALID_REQUESTED_QUANTITY"
    INVALID_ENTRY_PRICE = "INVALID_ENTRY_PRICE"
    INVALID_STOP_PRICE = "INVALID_STOP_PRICE"
    STOP_NOT_BELOW_ENTRY = "STOP_NOT_BELOW_ENTRY"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    # evidence and state
    GATE_MISSING = "GATE_MISSING"
    GATE_STALE = "GATE_STALE"
    GATE_MODE_MISMATCH = "GATE_MODE_MISMATCH"
    GATE_BLOCKED = "GATE_BLOCKED"
    ACCOUNT_EVIDENCE_MISSING = "ACCOUNT_EVIDENCE_MISSING"
    ACCOUNT_EVIDENCE_STALE = "ACCOUNT_EVIDENCE_STALE"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
    LEDGER_INCONSISTENT = "LEDGER_INCONSISTENT"
    PORTFOLIO_MISSING = "PORTFOLIO_MISSING"
    PERSISTENT_HALT = "PERSISTENT_HALT"
    RECONCILIATION_NOT_CLEAN = "RECONCILIATION_NOT_CLEAN"
    PORTFOLIO_NOT_ENTRY_ELIGIBLE = "PORTFOLIO_NOT_ENTRY_ELIGIBLE"
    EQUITY_UNKNOWN = "EQUITY_UNKNOWN"
    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    DAILY_LOSS_LOCKOUT = "DAILY_LOSS_LOCKOUT"
    WEEKLY_LIMIT_ACTIVE = "WEEKLY_LIMIT_ACTIVE"
    GAP_STRESS_UNAVAILABLE = "GAP_STRESS_UNAVAILABLE"
    LOSS_LIMIT_INELIGIBLE = "LOSS_LIMIT_INELIGIBLE"
    MARK_MISSING_OR_STALE = "MARK_MISSING_OR_STALE"
    MARK_BELOW_MINIMUM = "MARK_BELOW_MINIMUM"
    # portfolio rules
    UNBOOKED_FILLS = "UNBOOKED_FILLS"
    DUPLICATE_WORKING_ORDER = "DUPLICATE_WORKING_ORDER"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    POSITION_AT_CONCENTRATION_LIMIT = "POSITION_AT_CONCENTRATION_LIMIT"
    # sizing
    ONE_SHARE_EXCEEDS_RISK_BUDGET = "ONE_SHARE_EXCEEDS_RISK_BUDGET"
    QUANTITY_ZERO = "QUANTITY_ZERO"
    QUANTITY_EXCEEDS_LIMIT = "QUANTITY_EXCEEDS_LIMIT"


class Constraint(StrEnum):
    RISK_BUDGET = "RISK_BUDGET"
    POSITION_CONCENTRATION = "POSITION_CONCENTRATION"
    ORDER_COMMITMENT = "ORDER_COMMITMENT"
    CAPITAL_CEILING = "CAPITAL_CEILING"
    AVAILABLE_CASH = "AVAILABLE_CASH"
    GAP_STRESS_POSITION = "GAP_STRESS_POSITION"
    GAP_STRESS_PORTFOLIO = "GAP_STRESS_PORTFOLIO"
    BROKER_CASH_BACKSTOP = "BROKER_CASH_BACKSTOP"


@dataclass(frozen=True, slots=True, order=True)
class Rejection:
    code: RejectionCode
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConstraintCap:
    constraint: Constraint
    max_qty: int
    limit: Decimal
    used: Decimal
    """Amount already consumed before this order (e.g. existing exposure)."""


@dataclass(frozen=True, slots=True)
class SizingPreview:
    max_qty: int
    caps: tuple[ConstraintCap, ...]
    risk_per_share: Decimal

    @property
    def binding(self) -> tuple[Constraint, ...]:
        """The constraints that set ``max_qty`` (the limiting constraints)."""
        return tuple(c.constraint for c in self.caps if c.max_qty == self.max_qty)

    def cap(self, constraint: Constraint) -> ConstraintCap:
        return next(c for c in self.caps if c.constraint is constraint)


@dataclass(frozen=True, slots=True)
class RiskEvidence:
    evaluated_at: datetime
    ledger_fingerprint: str | None
    gate_evaluated_at: datetime | None
    gate_reason_codes: tuple[str, ...]
    mark_price: Decimal | None
    mark_as_of: datetime | None
    mark_source: str | None
    mark_feed: str | None
    account_id: str | None
    account_as_of: datetime | None
    equity: Decimal | None
    session_baseline_id: int | None
    week_baseline_id: int | None
    available_cash: Decimal | None
    capital_headroom: Decimal | None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    proposal: TradeProposal
    approved: bool
    approved_qty: Decimal
    reservation: Decimal
    planned_risk: Decimal
    rejections: tuple[Rejection, ...]
    sizing: SizingPreview | None
    evidence: RiskEvidence
    valid_until: datetime

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return tuple(sorted({r.code.value for r in self.rejections}))

    def still_valid(self, *, now: datetime, current_fingerprint: str) -> bool:
        """True only while unexpired AND the ledger state is unchanged. Step 12 must
        still re-evaluate inside the reserving transaction; this is a cheap pre-check."""
        return (
            self.approved
            and now <= self.valid_until
            and now >= self.evidence.evaluated_at
            and self.evidence.ledger_fingerprint == current_fingerprint
        )


@dataclass(frozen=True, slots=True)
class RiskEngineConfig:
    limits: RiskLimits
    mode: RuntimeMode
    fees: FeeModel
    marks: MarkFreshnessPolicy
    calendar_max_age: timedelta
    max_gate_age: timedelta = timedelta(seconds=30)
    max_account_age: timedelta = timedelta(seconds=120)
    reconciliation_max_age: timedelta = timedelta(minutes=10)
    decision_ttl: timedelta = timedelta(seconds=30)
    allowed_instruments: frozenset[InstrumentType] = field(
        default_factory=lambda: frozenset({InstrumentType.EQUITY, InstrumentType.ETF})
    )


# ------------------------------------------------------------------- sizing


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
    if denominator <= ZERO or numerator <= ZERO:
        return 0
    return int(DECIMAL_CONTEXT.divide(numerator, denominator).to_integral_value(ROUND_FLOOR))


def max_whole_qty(ok: Callable[[Decimal], bool], estimate: int) -> int:
    """Largest whole q >= 0 with ok(q) for a monotone (non-increasing truth) predicate.
    Starts at ``estimate`` and only ever moves to verified values; never rounds up an
    unsafe quantity."""
    q = max(estimate, 0)
    while q > 0 and not ok(Decimal(q)):
        q -= 1
    for _ in range(1000):
        if not ok(Decimal(q + 1)):
            return q
        q += 1
    return q  # pragma: no cover - estimates are within a few shares


@dataclass(frozen=True, slots=True)
class SizingInputs:
    entry: Decimal
    stop: Decimal
    equity: Decimal
    baseline_equity: Decimal
    held_market_value: Decimal
    pending_symbol_commitment: Decimal
    capital_headroom: Decimal
    available_cash: Decimal
    portfolio_stressed: Decimal
    broker_cash: Decimal


def size(inputs: SizingInputs, limits: RiskLimits, fees: FeeModel) -> SizingPreview:
    i = inputs
    rps = i.entry - i.stop
    gap = limits.gap_stress.scenario_gap_pct

    def reserve(q: Decimal) -> Decimal:
        return reservation_amount(q * i.entry + fees.buy_fee(q))

    risk_budget = limits.planned_risk_pct_equity * i.equity
    conc_limit = limits.max_position_pct_equity * i.equity
    exposure = i.held_market_value + i.pending_symbol_commitment
    pos_stress_limit = limits.gap_stress.max_position_scenario_loss_pct_equity * i.baseline_equity
    port_stress_limit = limits.gap_stress.max_portfolio_scenario_loss_pct_equity * i.baseline_equity
    specs: list[tuple[Constraint, Decimal, Decimal, Callable[[Decimal], bool], int]] = [
        (Constraint.RISK_BUDGET, risk_budget, ZERO,
         lambda q: q * rps + fees.buy_fee(q) + fees.sell_fee(q, i.stop) <= risk_budget,
         _floor_div(risk_budget, rps)),
        (Constraint.POSITION_CONCENTRATION, conc_limit, exposure,
         lambda q: exposure + q * i.entry <= conc_limit,
         _floor_div(conc_limit - exposure, i.entry)),
        (Constraint.ORDER_COMMITMENT, limits.max_order_notional, ZERO,
         lambda q: reserve(q) <= limits.max_order_notional,
         _floor_div(limits.max_order_notional, i.entry)),
        (Constraint.CAPITAL_CEILING, i.capital_headroom, ZERO,
         lambda q: reserve(q) <= i.capital_headroom,
         _floor_div(i.capital_headroom, i.entry)),
        (Constraint.AVAILABLE_CASH, i.available_cash, ZERO,
         lambda q: reserve(q) <= i.available_cash,
         _floor_div(i.available_cash, i.entry)),
        (Constraint.GAP_STRESS_POSITION, pos_stress_limit, exposure * gap,
         lambda q: (exposure + q * i.entry) * gap <= pos_stress_limit,
         _floor_div(pos_stress_limit - exposure * gap, i.entry * gap)),
        (Constraint.GAP_STRESS_PORTFOLIO, port_stress_limit, i.portfolio_stressed,
         lambda q: i.portfolio_stressed + q * i.entry * gap <= port_stress_limit,
         _floor_div(port_stress_limit - i.portfolio_stressed, i.entry * gap)),
        (Constraint.BROKER_CASH_BACKSTOP, i.broker_cash, ZERO,
         lambda q: q * i.entry + fees.buy_fee(q) <= i.broker_cash,
         _floor_div(i.broker_cash, i.entry)),
    ]  # fmt: skip
    caps = tuple(
        ConstraintCap(name, max_whole_qty(ok, estimate), limit, used)
        for name, limit, used, ok, estimate in specs
    )
    return SizingPreview(max_qty=min(c.max_qty for c in caps), caps=caps, risk_per_share=rps)


# ------------------------------------------------------------------- engine


_LOSS_CODES: Final = {
    LossBlocker.DAILY_LOSS_LOCKOUT: RejectionCode.DAILY_LOSS_LOCKOUT,
    LossBlocker.WEEKLY_LIMIT_ACTIVE: RejectionCode.WEEKLY_LIMIT_ACTIVE,
    LossBlocker.SESSION_BASELINE_MISSING: RejectionCode.BASELINE_UNAVAILABLE,
    LossBlocker.WEEK_BASELINE_MISSING: RejectionCode.BASELINE_UNAVAILABLE,
    LossBlocker.EQUITY_UNKNOWN: RejectionCode.EQUITY_UNKNOWN,
    LossBlocker.CALENDAR_UNAVAILABLE: RejectionCode.CALENDAR_UNAVAILABLE,
    # the portfolio already exceeding the gap scenario is caught by the sizing cap,
    # which then permits zero shares; it is reported there with the binding constraint.
    LossBlocker.GAP_STRESS_PORTFOLIO_EXCEEDED: None,
}


@dataclass(slots=True)
class _Evaluation:
    """Everything one evaluation read, in one ledger read session."""

    proposal: TradeProposal
    now: datetime
    gate: GateStatus | None
    account: AccountSnapshot | None
    rejections: list[Rejection]
    fingerprint: str | None = None
    snapshot: PortfolioSnapshot | None = None
    loss: LossLimitReport | None = None
    mark: PriceMark | None = None
    held_value: Decimal | None = ZERO
    pending_commitment: Decimal = ZERO

    def reject(self, code: RejectionCode, detail: str = "") -> None:
        self.rejections.append(Rejection(code, detail))


class PreTradeRiskEngine:
    """Read-only. Writes nothing, reserves nothing, submits nothing."""

    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        loss_limits: LossLimitMonitor,
        config: RiskEngineConfig,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._loss = loss_limits
        self._config = config

    def preview(
        self,
        proposal: TradeProposal,
        *,
        gate: GateStatus | None,
        account: AccountSnapshot | None,
        now: datetime,
    ) -> SizingPreview | None:
        """Maximum permitted shares and the limiting constraints (None when the inputs
        needed to size are unavailable). Advisory only: it authorizes nothing."""
        return self.evaluate(proposal, gate=gate, account=account, now=now).sizing

    @property
    def fees(self) -> FeeModel:
        return self._config.fees

    def fingerprint(self) -> str:
        with self._db.read() as s:
            return ledger_fingerprint(s)

    def evaluate(
        self,
        proposal: TradeProposal,
        *,
        gate: GateStatus | None,
        account: AccountSnapshot | None,
        now: datetime,
    ) -> RiskDecision:
        with self._db.read() as s:
            return self.evaluate_in(s, proposal, gate=gate, account=account, now=now)

    def evaluate_in(
        self,
        s: Session,
        proposal: TradeProposal,
        *,
        gate: GateStatus | None,
        account: AccountSnapshot | None,
        now: datetime,
    ) -> RiskDecision:
        """Evaluate against an already-open session. Step 12 calls this inside the
        ``BEGIN IMMEDIATE`` transaction that creates the order and its reservation, so
        the check and the reservation see exactly the same ledger state."""
        ev = _Evaluation(proposal, now, gate, account, [])
        prices_ok = self._check_shape(ev)
        self._check_gate(ev)
        self._check_account(ev)
        ev.fingerprint = ledger_fingerprint(s)
        if self._check_ledger(s, ev):
            self._check_market(s, ev)
            self._check_orders(s, ev)
        sizing = self._size(ev) if prices_ok else None
        return self._decision(ev, sizing)

    # ------------------------------------------------------------ proposal
    def _check_shape(self, ev: _Evaluation) -> bool:
        p, c = ev.proposal, self._config
        if p.side is not Side.BUY:
            ev.reject(RejectionCode.NOT_LONG_ENTRY, "only long entries")
        if p.order_type is not OrderType.LIMIT:
            ev.reject(RejectionCode.UNSUPPORTED_ORDER_TYPE, p.order_type.value)
        if p.extended_hours:
            ev.reject(RejectionCode.EXTENDED_HOURS_NOT_ALLOWED)
        if p.instrument_type not in c.allowed_instruments:
            ev.reject(RejectionCode.UNSUPPORTED_INSTRUMENT, p.instrument_type.value)
        if not p.symbol or not set(p.symbol) <= _SYMBOL_OK or len(p.symbol) > 9:
            ev.reject(RejectionCode.INVALID_SYMBOL, p.symbol)
        if not p.intent_key or len(p.intent_key) > 128:
            ev.reject(RejectionCode.INVALID_INTENT_KEY)
        q = p.requested_qty
        if q is not None and (not q.is_finite() or q <= ZERO or not is_whole(q)):
            ev.reject(RejectionCode.INVALID_REQUESTED_QUANTITY, f"{q} (whole shares only)")
        prices_ok = True
        for code, value in (
            (RejectionCode.INVALID_ENTRY_PRICE, p.entry_price),
            (RejectionCode.INVALID_STOP_PRICE, p.stop_price),
        ):
            try:
                valid_order_price(value)
            except MoneyError as exc:
                ev.reject(code, str(exc))
                prices_ok = False
        if prices_ok and p.stop_price >= p.entry_price:
            ev.reject(RejectionCode.STOP_NOT_BELOW_ENTRY, f"stop {p.stop_price} >= {p.entry_price}")
            prices_ok = False
        if prices_ok and p.entry_price < c.limits.min_price:
            ev.reject(RejectionCode.PRICE_BELOW_MINIMUM, f"{p.entry_price} < {c.limits.min_price}")
        return prices_ok

    # ------------------------------------------------------ external evidence
    def _check_gate(self, ev: _Evaluation) -> None:
        gate = ev.gate
        if gate is None:
            ev.reject(RejectionCode.GATE_MISSING)
            return
        if gate.mode is not self._config.mode:
            ev.reject(RejectionCode.GATE_MODE_MISMATCH, gate.mode.value)
        if not _fresh(gate.evaluated_at, ev.now, self._config.max_gate_age):
            ev.reject(RejectionCode.GATE_STALE, gate.evaluated_at.isoformat())
        if not gate.new_entries_allowed:
            ev.reject(RejectionCode.GATE_BLOCKED, ",".join(gate.reason_codes))

    def _check_account(self, ev: _Evaluation) -> None:
        account = ev.account
        if account is None:
            ev.reject(RejectionCode.ACCOUNT_EVIDENCE_MISSING)
            return
        if not _fresh(account.as_of, ev.now, self._config.max_account_age):
            ev.reject(RejectionCode.ACCOUNT_EVIDENCE_STALE, account.as_of.isoformat())
        if account.environment != EXPECTED_ENVIRONMENT[self._config.mode]:
            ev.reject(RejectionCode.ACCOUNT_MISMATCH, f"environment {account.environment}")
        if account.status != "ACTIVE" or account.trading_blocked or account.account_blocked:
            ev.reject(RejectionCode.ACCOUNT_RESTRICTED, account.status)

    # ---------------------------------------------------------------- ledger
    def _check_ledger(self, s: Session, ev: _Evaluation) -> bool:
        recorded = SyncStateRepository().get(s, BROKER_ACCOUNT_ID)
        if ev.account is not None and recorded != ev.account.account_id:
            ev.reject(RejectionCode.ACCOUNT_MISMATCH, "not the recorded broker account")
        integrity = integrity_report(s)
        if not integrity.ok:
            ev.reject(RejectionCode.LEDGER_INCONSISTENT, "; ".join(integrity.problems()))
        try:
            self._pf.portfolio(s)
        except PersistenceError:
            ev.reject(RejectionCode.PORTFOLIO_MISSING)
            return False
        today = market_date(ev.now)
        invariants = accounting_checker(lambda: today).run(s)
        if not invariants.ok:
            names = sorted({v.invariant for v in invariants.violations})
            ev.reject(RejectionCode.LEDGER_INCONSISTENT, ", ".join(names))
        state = TradingStateRepository().get(s)
        if state.halted:
            ev.reject(RejectionCode.PERSISTENT_HALT, str(state.reason_code))
        self._check_reconciliation(s, ev)

        ev.snapshot = snap = self._pf.snapshot(s, now=ev.now)
        ev.loss = loss = self._loss.report(s, now=ev.now)
        if snap.entry_blockers:
            ev.reject(RejectionCode.PORTFOLIO_NOT_ENTRY_ELIGIBLE,
                      ",".join(b.value for b in snap.entry_blockers))  # fmt: skip
        for blocker in loss.blockers:
            code = _LOSS_CODES.get(blocker, RejectionCode.LOSS_LIMIT_INELIGIBLE)
            if code is not None:
                ev.reject(code, blocker.value)
        if snap.equity is None or snap.equity <= ZERO:
            ev.reject(RejectionCode.EQUITY_UNKNOWN)
        if loss.daily_baseline is None or loss.weekly_baseline is None:
            ev.reject(RejectionCode.BASELINE_UNAVAILABLE)
        if loss.gap_stress is None:
            ev.reject(RejectionCode.GAP_STRESS_UNAVAILABLE)
        return True

    def _check_reconciliation(self, s: Session, ev: _Evaluation) -> None:
        latest = ReconciliationRunsRepository().latest(s)
        if latest is None:
            ev.reject(RejectionCode.RECONCILIATION_NOT_CLEAN, "no reconciliation")
        elif latest.result is not ReconciliationResult.OK:
            ev.reject(RejectionCode.RECONCILIATION_NOT_CLEAN, latest.result.value)
        elif not _fresh(latest.ts, ev.now, self._config.reconciliation_max_age):
            ev.reject(RejectionCode.RECONCILIATION_NOT_CLEAN, f"stale {latest.ts.isoformat()}")

    def _check_market(self, s: Session, ev: _Evaluation) -> None:
        c = self._config
        mark = PriceMarksRepository().get(s, ev.proposal.symbol)
        try:
            calendar = CalendarStore().calendar(s, as_of=ev.now, max_age=c.calendar_max_age)
        except CalendarError:
            calendar = None
            ev.reject(RejectionCode.CALENDAR_UNAVAILABLE)
        assessment = assess_mark(mark, now=ev.now, calendar=calendar, policy=c.marks)
        if mark is None or not assessment.fresh:
            ev.reject(RejectionCode.MARK_MISSING_OR_STALE, assessment.reason)
            return
        ev.mark = mark
        if mark.price < c.limits.min_price:
            ev.reject(RejectionCode.MARK_BELOW_MINIMUM, f"{mark.price} < {c.limits.min_price}")

    def _check_orders(self, s: Session, ev: _Evaluation) -> None:
        p, c, snap = ev.proposal, self._config, ev.snapshot
        assert snap is not None  # noqa: S101 - set by _check_ledger
        working = OrdersRepository().list_working(s)
        same_symbol = [o for o in working if o.symbol == p.symbol]
        if same_symbol:
            ev.reject(RejectionCode.DUPLICATE_WORKING_ORDER,
                      ",".join(o.client_order_id for o in same_symbol))  # fmt: skip
        unbooked = OrdersRepository().list_with_unbooked_fills(s)
        if unbooked:
            # A broker-reported execution is not booked yet: equity, cash, exposure and
            # stress are all unknown until it is. No new risk.
            detail = ",".join(f"{o.client_order_id}+{o.unbooked_fill_qty}" for o in unbooked)
            ev.reject(RejectionCode.UNBOOKED_FILLS, detail)
        if s.fetch_one("SELECT 1 FROM orders WHERE intent_key = ?", (p.intent_key,)):
            ev.reject(RejectionCode.DUPLICATE_INTENT, p.intent_key)

        held = {v.symbol: v for v in snap.positions if v.qty > ZERO}
        pending = {o.symbol for o in working if o.side is Side.BUY}
        occupied = set(held) | pending
        if p.symbol not in occupied and len(occupied) >= c.limits.max_open_positions:
            limit = c.limits.max_open_positions
            ev.reject(
                RejectionCode.MAX_POSITIONS_REACHED, f"{len(occupied)} held/pending of {limit}"
            )

        reservations = ReservationsRepository()
        for order in same_symbol:
            if order.side is Side.BUY:
                r = reservations.get(s, order.client_order_id)
                ev.pending_commitment += ZERO if r is None else r.remaining
        if p.symbol in held:
            ev.held_value = held[p.symbol].market_value  # None if the holding is unvalued
            if ev.held_value is None:
                ev.reject(RejectionCode.MARK_MISSING_OR_STALE, "existing holding unvalued")

    # ---------------------------------------------------------------- sizing
    def _size(self, ev: _Evaluation) -> SizingPreview | None:
        snap, loss, account = ev.snapshot, ev.loss, ev.account
        if (
            snap is None or snap.equity is None or snap.equity <= ZERO
            or loss is None or loss.gap_stress is None
            or account is None or ev.held_value is None
        ):  # fmt: skip
            return None
        sizing = size(
            SizingInputs(
                entry=ev.proposal.entry_price,
                stop=ev.proposal.stop_price,
                equity=snap.equity,
                baseline_equity=loss.gap_stress.denominator,
                held_market_value=ev.held_value,
                pending_symbol_commitment=ev.pending_commitment,
                capital_headroom=snap.balances.capital_headroom,
                available_cash=snap.balances.available_cash,
                portfolio_stressed=loss.gap_stress.portfolio_stressed,
                broker_cash=broker_cash_backstop(account),
            ),
            self._config.limits,
            self._config.fees,
        )
        conc = sizing.cap(Constraint.POSITION_CONCENTRATION)
        if conc.used >= conc.limit:
            ev.reject(RejectionCode.POSITION_AT_CONCENTRATION_LIMIT, f"{conc.used} >= {conc.limit}")
        return sizing

    # -------------------------------------------------------------- decision
    def _decision(self, ev: _Evaluation, sizing: SizingPreview | None) -> RiskDecision:
        c, p = self._config, ev.proposal
        qty = ZERO
        if sizing is not None:
            binding = ",".join(b.value for b in sizing.binding)
            if sizing.max_qty == 0:
                one_share_too_risky = sizing.cap(Constraint.RISK_BUDGET).max_qty == 0
                ev.reject(RejectionCode.ONE_SHARE_EXCEEDS_RISK_BUDGET if one_share_too_risky
                          else RejectionCode.QUANTITY_ZERO, binding)  # fmt: skip
            elif p.requested_qty is not None and p.requested_qty > sizing.max_qty:
                ev.reject(RejectionCode.QUANTITY_EXCEEDS_LIMIT,
                          f"{p.requested_qty} > {sizing.max_qty} ({binding})")  # fmt: skip
            qty = Decimal(sizing.max_qty) if p.requested_qty is None else p.requested_qty
        rejections = tuple(sorted(set(ev.rejections)))
        approved = not rejections and sizing is not None and qty > ZERO
        if not approved:
            qty = ZERO
        fees = c.fees
        reservation = (
            reservation_amount(qty * p.entry_price + fees.buy_fee(qty)) if approved else ZERO
        )
        planned = (qty * (p.entry_price - p.stop_price) + fees.buy_fee(qty)
                   + fees.sell_fee(qty, p.stop_price))  # fmt: skip
        evidence = self._evidence(ev)
        payload = json.dumps([p.intent_key, p.symbol, str(p.entry_price), str(p.stop_price),
                              str(p.requested_qty), str(qty), ev.fingerprint,
                              ev.now.isoformat(), [r.code.value for r in rejections]])  # fmt: skip
        return RiskDecision(
            decision_id=hashlib.sha256(payload.encode()).hexdigest()[:32],
            proposal=p,
            approved=approved,
            approved_qty=qty,
            reservation=reservation,
            planned_risk=planned,
            rejections=rejections,
            sizing=sizing,
            evidence=evidence,
            valid_until=self._valid_until(ev),
        )

    @staticmethod
    def _evidence(ev: _Evaluation) -> RiskEvidence:
        gate, account, mark, snap, loss = ev.gate, ev.account, ev.mark, ev.snapshot, ev.loss
        daily = None if loss is None else loss.daily_baseline
        weekly = None if loss is None else loss.weekly_baseline
        return RiskEvidence(
            evaluated_at=ev.now,
            ledger_fingerprint=ev.fingerprint,
            gate_evaluated_at=None if gate is None else gate.evaluated_at,
            gate_reason_codes=() if gate is None else gate.reason_codes,
            mark_price=None if mark is None else mark.price,
            mark_as_of=None if mark is None else mark.as_of,
            mark_source=None if mark is None else mark.source.value,
            mark_feed=None if mark is None else mark.feed,
            account_id=None if account is None else account.account_id,
            account_as_of=None if account is None else account.as_of,
            equity=None if snap is None else snap.equity,
            session_baseline_id=None if daily is None else daily.id,
            week_baseline_id=None if weekly is None else weekly.id,
            available_cash=None if snap is None else snap.balances.available_cash,
            capital_headroom=None if snap is None else snap.balances.capital_headroom,
        )

    def _valid_until(self, ev: _Evaluation) -> datetime:
        """The earliest moment any piece of evidence stops being acceptable."""
        c = self._config
        expiries = [ev.now + c.decision_ttl]
        if ev.gate is not None:
            expiries.append(ev.gate.evaluated_at + c.max_gate_age)
        if ev.account is not None:
            expiries.append(ev.account.as_of + c.max_account_age)
        if ev.mark is not None:
            expiries.append(ev.mark.as_of + c.marks.max_age_in_session)
            expiries.append(ev.mark.received_at + c.marks.max_receipt_age)
        return min(expiries)


def broker_cash_backstop(account: AccountSnapshot) -> Decimal:
    """The broker-side cap used ONLY to lower a size: the smaller of the account's cash
    and non-marginable buying power, never below zero. Margin ``buying_power`` is not
    part of :class:`AccountSnapshot` at all, and this value never replaces virtual
    settled cash or the $5,000 deployed-capital ceiling (those are separate caps)."""
    return max(ZERO, min(account.cash, account.non_marginable_buying_power))


def _fresh(at: datetime, now: datetime, max_age: timedelta) -> bool:
    """Not in the future and no older than ``max_age``."""
    return timedelta(0) <= now - at <= max_age


__all__ = [
    "Constraint",
    "ConstraintCap",
    "PreTradeRiskEngine",
    "Rejection",
    "RejectionCode",
    "RiskDecision",
    "RiskEngineConfig",
    "RiskEvidence",
    "SizingInputs",
    "SizingPreview",
    "TradeProposal",
    "broker_cash_backstop",
    "max_whole_qty",
    "size",
]
