"""Virtual portfolio accounting engine (ARCHITECTURE §6, M1 step 6).

Responsibilities: the $5,000 cash journal, settled/unsettled cash, buy reservations,
average-cost positions, realized/unrealized P&L, equity, the fixed deployed-capital
ceiling, buying capacity, fees and dividends, price marks and exposure reporting,
replay-safe fill processing and post-commit invariant checks.

Out of scope here: order status lifecycle (step 12), loss limits (step 7), risk gate
(step 11), broker I/O. Nothing in this module performs network I/O.

Broker events
-------------
``process_fill`` / ``process_cash_activity`` each run **one** write transaction:
activity record → fill → cash entry → position → reservation → order fill totals →
audit event (plus any halt). Then, after commit, the full invariant checker runs and
any violation is persisted as a ``LEDGER_INVARIANT`` halt. A confirmed broker event is
therefore always recorded first — even when it breaks an invariant — and the halt
follows. Replays of the same activity are exact no-ops; a replay with different
content is a ``CONFLICT`` and halts without touching the books.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from swing_trading_bot.domain.accounting import (
    PositionState,
    apply_buy,
    apply_sell,
    average_price,
    buy_cost,
    sell_proceeds,
)
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarError, TradingCalendar
from swing_trading_bot.domain.enums import (
    ActivityAttribution,
    HaltReason,
    IntentPurpose,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import is_bot_client_order_id
from swing_trading_bot.domain.models import PriceMark
from swing_trading_bot.domain.money import ZERO, reservation_amount
from swing_trading_bot.domain.state_machine import TERMINAL_STATUSES
from swing_trading_bot.domain.valuation import (
    MarkAssessment,
    MarkFreshnessPolicy,
    MarkStatus,
    assess_mark,
)
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.balances import Balances, compute_balances
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database, Session, Tx
from swing_trading_bot.ledger.errors import PersistenceError, RecordNotFoundError
from swing_trading_bot.ledger.invariants import InvariantReport, Violation
from swing_trading_bot.ledger.repositories import (
    ActivitiesRepository,
    ActivityRecord,
    CashEntriesRepository,
    CashEntryKind,
    CashEntryRecord,
    FillRecord,
    FillsRepository,
    IngestResult,
    OrderEvent,
    OrderEventsRepository,
    OrderRecord,
    OrdersRepository,
    PortfolioRecord,
    PortfoliosRepository,
    PositionRecord,
    PositionsRepository,
    PriceMarksRepository,
    ReservationsRepository,
    ReservationStatus,
    TradingStateRepository,
)

ACCOUNTING_ACTOR = "accounting"


class AccountingError(PersistenceError):
    pass


class InsufficientBuyingCapacityError(AccountingError):
    pass


class FillOutcome(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    UNATTRIBUTED = "UNATTRIBUTED"
    ORPHAN = "ORPHAN"


class CashActivityKind(StrEnum):
    FEE = "FEE"
    DIVIDEND = "DIVIDEND"


@dataclass(frozen=True, slots=True)
class ConfirmedFill:
    """A broker-confirmed execution, already parsed into exact values."""

    activity_id: str
    client_order_id: str | None
    broker_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fees: Decimal
    transaction_time: datetime
    raw_json: str

    @property
    def net_cash(self) -> Decimal:
        if self.side is Side.BUY:
            return -buy_cost(self.qty, self.price, self.fees)
        return sell_proceeds(self.qty, self.price, self.fees)


@dataclass(frozen=True, slots=True)
class CashActivity:
    """A broker non-trade cash activity attributable to the portfolio (fee, dividend)."""

    activity_id: str
    kind: CashActivityKind
    amount: Decimal  # signed, as reported by the broker (fees negative, dividends positive)
    occurred_at: datetime
    raw_json: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outcome: FillOutcome
    halted: bool
    halt_reasons: tuple[HaltReason, ...] = ()
    violations: tuple[Violation, ...] = ()
    fill_id: int | None = None
    settles_on: date | None = None


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    portfolio_name: str
    calendar_max_age: timedelta
    marks: MarkFreshnessPolicy


@dataclass(frozen=True, slots=True)
class PositionValuation:
    symbol: str
    qty: Decimal
    cost_basis: Decimal
    avg_cost: Decimal | None
    realized_pnl: Decimal
    mark: PriceMark | None
    mark_assessment: MarkAssessment
    market_value: Decimal | None
    unrealized_pnl: Decimal | None


class EntryBlocker(StrEnum):
    STALE_OR_MISSING_MARKS = "STALE_OR_MISSING_MARKS"
    UNRESOLVED_SETTLEMENT = "UNRESOLVED_SETTLEMENT"
    NEGATIVE_AVAILABLE_CASH = "NEGATIVE_AVAILABLE_CASH"
    CAPITAL_CEILING_EXCEEDED = "CAPITAL_CEILING_EXCEEDED"
    NEGATIVE_POSITION = "NEGATIVE_POSITION"
    NO_BUYING_CAPACITY = "NO_BUYING_CAPACITY"


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    as_of: datetime
    balances: Balances
    positions: tuple[PositionValuation, ...]
    realized_pnl: Decimal
    fill_fees: Decimal
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    entry_blockers: tuple[EntryBlocker, ...]

    @property
    def equity(self) -> Decimal | None:
        """cash + market value; None when any held position lacks a fresh mark."""
        return None if self.market_value is None else self.balances.cash + self.market_value

    @property
    def market_exposure(self) -> Decimal | None:
        """Market value of positions plus working buy commitments (U8)."""
        if self.market_value is None:
            return None
        return self.market_value + self.balances.buy_commitments

    @property
    def entry_eligible(self) -> bool:
        return not self.entry_blockers


HookFn = Callable[[str], None]


def market_date(ts: datetime) -> date:
    return ts.astimezone(MARKET_TZ).date()


class VirtualPortfolio:
    def __init__(self, db: Database, config: PortfolioConfig) -> None:
        self._db = db
        self._config = config
        self._portfolios = PortfoliosRepository()
        self._orders = OrdersRepository()
        self._events = OrderEventsRepository()
        self._activities = ActivitiesRepository()
        self._fills = FillsRepository()
        self._cash = CashEntriesRepository()
        self._positions = PositionsRepository()
        self._reservations = ReservationsRepository()
        self._marks = PriceMarksRepository()
        self._state = TradingStateRepository()
        self._calendars = CalendarStore()

    # ----------------------------------------------------------------- setup
    @staticmethod
    def initialize(
        tx: Tx,
        *,
        name: str,
        mode: RuntimeMode,
        initial_capital: Decimal,
        max_capital_allocation: Decimal,
        account_profile: str,
        at: datetime,
    ) -> int:
        """Create the portfolio row and its single settled opening DEPOSIT."""
        repo = PortfoliosRepository()
        if repo.get_by_name(tx, name) is not None:
            raise AccountingError(f"portfolio {name!r} already exists")
        if initial_capital <= ZERO or max_capital_allocation <= ZERO:
            raise AccountingError("capital values must be positive")
        pid = repo.insert(
            tx,
            PortfolioRecord(
                name=name,
                mode=mode,
                initial_capital=initial_capital,
                max_capital_allocation=max_capital_allocation,
                account_profile=account_profile,
                created_at=at,
            ),
        )
        CashEntriesRepository().append(
            tx,
            CashEntryRecord(
                portfolio_id=pid,
                ts=at,
                kind=CashEntryKind.DEPOSIT,
                amount=initial_capital,
                trade_date=market_date(at),
                settles_on=market_date(at),
                memo="initial virtual capital",
            ),
        )
        return pid

    def portfolio(self, s: Session) -> PortfolioRecord:
        rec = self._portfolios.get_by_name(s, self._config.portfolio_name)
        if rec is None or rec.id is None:
            raise RecordNotFoundError(f"portfolio {self._config.portfolio_name!r} not found")
        return rec

    def _pid(self, s: Session) -> int:
        rec = self.portfolio(s)
        if rec.id is None:  # pragma: no cover - stored rows always have ids
            raise RecordNotFoundError("portfolio has no id")
        return rec.id

    def balances(self, s: Session, *, now: datetime) -> Balances:
        return compute_balances(s, self._pid(s), market_date(now))

    # ---------------------------------------------------------- reservations
    def reserve_buy(
        self, tx: Tx, client_order_id: str, *, fee_estimate: Decimal, now: datetime
    ) -> Decimal:
        """Reserve qty x limit + fee estimate (rounded up) for a working buy order.

        Reservations reduce available cash but not cash. Refuses if the amount exceeds
        buying capacity = min(settled available cash, capital headroom).
        """
        order = self._orders.require(tx, client_order_id)
        if order.side is not Side.BUY or order.limit_price is None:
            raise AccountingError("only limit buy orders hold cash reservations")
        if order.status is not OrderStatus.PENDING_SUBMIT:
            raise AccountingError("reservations are created before submission only")
        if fee_estimate < ZERO:
            raise AccountingError("fee estimate must not be negative")
        amount = reservation_amount(order.qty * order.limit_price + fee_estimate)
        capacity = compute_balances(tx, order.portfolio_id, market_date(now)).buying_capacity
        if amount > capacity:
            raise InsufficientBuyingCapacityError(
                f"reservation {amount} exceeds buying capacity {capacity}"
            )
        self._reservations.create(tx, client_order_id, amount, now)
        return amount

    def close_reservation(self, tx: Tx, client_order_id: str, *, now: datetime) -> Decimal:
        """Close the reservation of an order that has reached a broker-confirmed terminal
        status (the caller records that status first, in the same transaction).

        FILLED consumes the reservation; any other terminal status releases it. The
        remainder returns to available cash and is returned; no-op if not ACTIVE. Refuses
        while the order is still working, so a reservation is never released early.
        """
        order = self._orders.require(tx, client_order_id)
        if order.status not in TERMINAL_STATUSES:
            raise AccountingError(
                f"order {client_order_id} is {order.status.value}; reservations close only "
                "after a broker-confirmed terminal status"
            )
        res = self._reservations.get(tx, client_order_id)
        if res is None or res.status is not ReservationStatus.ACTIVE:
            return ZERO
        status = (
            ReservationStatus.CONSUMED
            if order.status is OrderStatus.FILLED
            else ReservationStatus.RELEASED
        )
        self._reservations.close(tx, client_order_id, status, now)
        return res.remaining

    # ----------------------------------------------------------- broker fills
    def process_fill(
        self, fill: ConfirmedFill, *, now: datetime, hook: HookFn | None = None
    ) -> ProcessResult:
        with self._db.transaction() as tx:
            result = self._book_fill(tx, fill, now=now, hook=hook)
        return self._post_commit(result, now=now)

    def _book_fill(
        self, tx: Tx, fill: ConfirmedFill, *, now: datetime, hook: HookFn | None
    ) -> ProcessResult:
        bot_order = fill.client_order_id is not None and is_bot_client_order_id(
            fill.client_order_id
        )
        recorded = self._activities.record(
            tx,
            ActivityRecord(
                activity_id=fill.activity_id,
                activity_type="FILL",
                ts=fill.transaction_time,
                attribution=(
                    ActivityAttribution.ATTRIBUTABLE
                    if bot_order
                    else ActivityAttribution.UNEXPLAINED
                ),
                raw_json=fill.raw_json,
                ingested_at=now,
                symbol=fill.symbol,
                qty=fill.qty,
                price=fill.price,
                net_amount=fill.net_cash,
                side=fill.side,
                broker_order_id=fill.broker_order_id,
                client_order_id=fill.client_order_id,
            ),
        )
        if recorded is IngestResult.DUPLICATE:
            existing = self._fills.get_by_activity(tx, fill.activity_id)
            return ProcessResult(
                FillOutcome.DUPLICATE,
                halted=False,
                fill_id=None if existing is None else existing.id,
            )
        if recorded is IngestResult.CONFLICT:
            return self._recon_halt(tx, FillOutcome.CONFLICT, fill.activity_id, now)
        if not bot_order or fill.client_order_id is None:
            return self._recon_halt(tx, FillOutcome.UNATTRIBUTED, fill.activity_id, now)
        if hook:
            hook("after_activity")
        return self._book_attributed_fill(tx, fill, fill.client_order_id, now=now, hook=hook)

    def _recon_halt(
        self, tx: Tx, outcome: FillOutcome, activity_id: str, now: datetime
    ) -> ProcessResult:
        why = {
            FillOutcome.CONFLICT: "replayed with different content",
            FillOutcome.UNATTRIBUTED: "is a fill for a non-bot order",
        }[outcome]
        self._halt(tx, HaltReason.RECON_EXECUTION, f"activity {activity_id} {why}", now)
        return ProcessResult(outcome, halted=True, halt_reasons=(HaltReason.RECON_EXECUTION,))

    def _book_attributed_fill(
        self,
        tx: Tx,
        fill: ConfirmedFill,
        client_order_id: str,
        *,
        now: datetime,
        hook: HookFn | None,
    ) -> ProcessResult:

        order = self._orders.get(tx, client_order_id)
        orphan = order is None
        if order is None:
            order = self._create_orphan(tx, fill, client_order_id, now)

        pos_rec = self._positions.get(tx, order.portfolio_id, fill.symbol)
        before = (
            PositionState.flat()
            if pos_rec is None
            else PositionState(pos_rec.qty, pos_rec.cost_basis, pos_rec.realized_pnl)
        )
        realized: Decimal | None = None
        avg_before: Decimal | None = before.avg_cost
        if fill.side is Side.BUY:
            after = apply_buy(before, fill.qty, fill.price, fill.fees)
        else:
            sold = apply_sell(before, fill.qty, fill.price, fill.fees)
            after, realized, avg_before = sold.position, sold.realized_pnl, sold.avg_cost_before

        _, fill_id = self._fills.insert(
            tx,
            FillRecord(
                activity_id=fill.activity_id,
                client_order_id=client_order_id,
                symbol=fill.symbol,
                side=fill.side,
                qty=fill.qty,
                price=fill.price,
                fees=fill.fees,
                filled_at=fill.transaction_time,
                recorded_at=now,
                avg_cost_before=avg_before,
                realized_pnl=realized,
            ),
        )
        if hook:
            hook("after_fill")

        trade_date = market_date(fill.transaction_time)
        settles_on = self._settlement_date(tx, trade_date, now)
        self._cash.append(
            tx,
            CashEntryRecord(
                portfolio_id=order.portfolio_id,
                ts=fill.transaction_time,
                kind=CashEntryKind.BUY if fill.side is Side.BUY else CashEntryKind.SELL,
                amount=fill.net_cash,
                trade_date=trade_date,
                settles_on=settles_on,
                activity_id=fill.activity_id,
                fill_id=fill_id,
            ),
        )
        if hook:
            hook("after_cash")

        opening = before.qty <= ZERO < after.qty
        still_open = after.qty > ZERO
        self._positions.upsert(
            tx,
            PositionRecord(
                portfolio_id=order.portfolio_id,
                symbol=fill.symbol,
                qty=after.qty,
                cost_basis=after.cost_basis,
                realized_pnl=after.realized_pnl,
                updated_at=now,
                opened_at=(
                    fill.transaction_time
                    if opening
                    else (pos_rec.opened_at if pos_rec is not None and still_open else None)
                ),
                opened_session=(
                    trade_date
                    if opening
                    else (pos_rec.opened_session if pos_rec is not None and still_open else None)
                ),
            ),
        )
        if hook:
            hook("after_position")

        if fill.side is Side.BUY and order.limit_price is not None:
            res = self._reservations.get(tx, order.client_order_id)
            if res is not None and res.status is ReservationStatus.ACTIVE:
                remaining = max(ZERO, res.remaining - fill.qty * order.limit_price)
                self._reservations.set_remaining(tx, order.client_order_id, remaining, now)

        order_fills = self._fills.list_for_order(tx, order.client_order_id)
        total_qty = sum((f.qty for f in order_fills), ZERO)
        notional = sum((f.qty * f.price for f in order_fills), ZERO)
        self._orders.record_fill_progress(
            tx,
            order.client_order_id,
            filled_qty=total_qty,
            avg_fill_price=average_price(notional, total_qty),
            at=now,
        )
        self._events.append(
            tx,
            OrderEvent(
                client_order_id=order.client_order_id,
                ts=now,
                source="BROKER",
                detail={
                    "event": "FILL",
                    "activity_id": fill.activity_id,
                    "qty": fill.qty,
                    "price": fill.price,
                    "fees": fill.fees,
                    "settles_on": None if settles_on is None else settles_on.isoformat(),
                },
            ),
        )
        if hook:
            hook("before_commit")
        if orphan:
            self._halt(tx, HaltReason.RECON_EXECUTION,
                       f"fill {fill.activity_id} for unknown bot order {client_order_id}",
                       now)  # fmt: skip
            return ProcessResult(FillOutcome.ORPHAN, halted=True, fill_id=fill_id,
                                 halt_reasons=(HaltReason.RECON_EXECUTION,),
                                 settles_on=settles_on)  # fmt: skip
        return ProcessResult(FillOutcome.APPLIED, halted=False, fill_id=fill_id,
                             settles_on=settles_on)  # fmt: skip

    def _create_orphan(
        self, tx: Tx, fill: ConfirmedFill, client_order_id: str, now: datetime
    ) -> OrderRecord:
        rec = OrderRecord(
            client_order_id=client_order_id,
            portfolio_id=self._pid(tx),
            intent_key=f"orphan:{client_order_id}",
            symbol=fill.symbol,
            side=fill.side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            purpose=IntentPurpose.ENTRY if fill.side is Side.BUY else IntentPurpose.RISK_EXIT,
            qty=fill.qty,
            limit_price=None,
            planned_stop=None,
            status=OrderStatus.ORPHAN,
            filled_qty=ZERO,
            avg_fill_price=None,
            created_at=now,
            updated_at=now,
            broker_order_id=fill.broker_order_id,
        )
        self._orders.insert(tx, rec)
        return rec

    def _settlement_date(self, s: Session, trade_date: date, now: datetime) -> date | None:
        """Authoritative settlement date, or None (unresolved -> proceeds unavailable)."""
        try:
            calendar = self._calendars.calendar(s, as_of=now, max_age=self._config.calendar_max_age)
            return calendar.settlement_date(trade_date)
        except CalendarError:
            return None

    # ------------------------------------------------------ fees / dividends
    def process_cash_activity(self, act: CashActivity, *, now: datetime) -> ProcessResult:
        with self._db.transaction() as tx:
            recorded = self._activities.record(
                tx,
                ActivityRecord(
                    activity_id=act.activity_id,
                    activity_type=act.kind.value,
                    ts=act.occurred_at,
                    attribution=ActivityAttribution.ATTRIBUTABLE,
                    raw_json=act.raw_json,
                    ingested_at=now,
                    symbol=act.symbol,
                    net_amount=act.amount,
                ),
            )
            if recorded is IngestResult.DUPLICATE:
                result = ProcessResult(FillOutcome.DUPLICATE, halted=False)
            elif recorded is IngestResult.CONFLICT:
                result = self._recon_halt(tx, FillOutcome.CONFLICT, act.activity_id, now)
            else:
                day = market_date(act.occurred_at)
                self._cash.append(
                    tx,
                    CashEntryRecord(
                        portfolio_id=self._pid(tx),
                        ts=act.occurred_at,
                        kind=(
                            CashEntryKind.FEE
                            if act.kind is CashActivityKind.FEE
                            else CashEntryKind.DIVIDEND
                        ),
                        amount=act.amount,
                        trade_date=day,
                        settles_on=day,  # cash movements, available immediately
                        activity_id=act.activity_id,
                        memo=None if act.symbol is None else f"{act.kind.value} {act.symbol}",
                    ),
                )
                result = ProcessResult(FillOutcome.APPLIED, halted=False)
        return self._post_commit(result, now=now)

    # ------------------------------------------------------------ settlement
    def resolve_settlements(self, *, now: datetime) -> int:
        """Fill in settlement dates that could not be determined at booking time."""
        resolved = 0
        with self._db.transaction() as tx:
            for entry in self._cash.list_unresolved_settlements(tx, self._pid(tx)):
                settles_on = self._settlement_date(tx, entry.trade_date, now)
                if settles_on is not None and entry.id is not None:
                    self._cash.resolve_settlement(tx, entry.id, settles_on)
                    resolved += 1
        return resolved

    # ----------------------------------------------------------- invariants
    def _post_commit(self, result: ProcessResult, *, now: datetime) -> ProcessResult:
        report = self.check_invariants(now=now)
        if report.ok:
            return result
        reasons = (*result.halt_reasons, HaltReason.LEDGER_INVARIANT)
        return ProcessResult(
            outcome=result.outcome,
            halted=True,
            halt_reasons=reasons,
            violations=report.violations,
            fill_id=result.fill_id,
            settles_on=result.settles_on,
        )

    def check_invariants(self, *, now: datetime) -> InvariantReport:
        """Run all data + accounting invariants; persist a halt if any are violated."""
        today = market_date(now)
        with self._db.read() as s:
            report = accounting_checker(lambda: today).run(s)
        if not report.ok:
            summary = "; ".join(f"{v.invariant}[{v.subject}]: {v.detail}"
                                for v in report.violations[:5])  # fmt: skip
            with self._db.transaction() as tx:
                current = self._state.get(tx)
                already = current.halted and current.reason_code == HaltReason.LEDGER_INVARIANT
                if not (already and current.reason_detail == summary):
                    self._halt(tx, HaltReason.LEDGER_INVARIANT, summary, now)
        return report

    def _halt(self, tx: Tx, reason: HaltReason, detail: str, now: datetime) -> None:
        self._state.halt(tx, reason, detail=detail[:2000], actor=ACCOUNTING_ACTOR, at=now)

    # ------------------------------------------------------------- valuation
    def record_mark(self, tx: Tx, mark: PriceMark) -> None:
        self._marks.upsert(tx, mark)

    def snapshot(self, s: Session, *, now: datetime) -> PortfolioSnapshot:
        pid = self._pid(s)
        balances = compute_balances(s, pid, market_date(now))
        try:
            calendar: TradingCalendar | None = self._calendars.calendar(
                s, as_of=now, max_age=self._config.calendar_max_age
            )
        except CalendarError:
            calendar = None
        valuations: list[PositionValuation] = []
        all_fresh = True
        realized_total = ZERO
        for p in self._positions.list_for_portfolio(s, pid):
            realized_total += p.realized_pnl
            state = PositionState(p.qty, p.cost_basis, p.realized_pnl)
            if p.qty == ZERO:
                valuations.append(
                    PositionValuation(p.symbol, p.qty, p.cost_basis, None, p.realized_pnl, None,
                                      MarkAssessment(MarkStatus.FRESH, "flat position"),
                                      ZERO, ZERO)
                )  # fmt: skip
                continue
            mark = self._marks.get(s, p.symbol)
            assessment = assess_mark(mark, now=now, calendar=calendar, policy=self._config.marks)
            if assessment.fresh and mark is not None:
                mv: Decimal | None = p.qty * mark.price
                unrealized: Decimal | None = p.qty * mark.price - p.cost_basis
            else:
                mv = unrealized = None
                all_fresh = False
            valuations.append(
                PositionValuation(p.symbol, p.qty, p.cost_basis, state.avg_cost, p.realized_pnl,
                                  mark, assessment, mv, unrealized)
            )  # fmt: skip
        fill_fees = sum((f.fees for f in self._fills.list_in_application_order(s)), ZERO)
        market_value = (
            sum((v.market_value for v in valuations if v.market_value is not None), ZERO)
            if all_fresh
            else None
        )
        unrealized_total = (
            sum((v.unrealized_pnl for v in valuations if v.unrealized_pnl is not None), ZERO)
            if all_fresh
            else None
        )
        blockers: list[EntryBlocker] = []
        if not all_fresh:
            blockers.append(EntryBlocker.STALE_OR_MISSING_MARKS)
        if balances.unresolved_settlements:
            blockers.append(EntryBlocker.UNRESOLVED_SETTLEMENT)
        if balances.available_cash < ZERO:
            blockers.append(EntryBlocker.NEGATIVE_AVAILABLE_CASH)
        if balances.deployed_capital > balances.max_capital_allocation:
            blockers.append(EntryBlocker.CAPITAL_CEILING_EXCEEDED)
        if any(v.qty < ZERO for v in valuations):
            blockers.append(EntryBlocker.NEGATIVE_POSITION)
        if balances.buying_capacity <= ZERO:
            blockers.append(EntryBlocker.NO_BUYING_CAPACITY)
        return PortfolioSnapshot(
            as_of=now,
            balances=balances,
            positions=tuple(valuations),
            realized_pnl=realized_total,
            fill_fees=fill_fees,
            market_value=market_value,
            unrealized_pnl=unrealized_total,
            entry_blockers=tuple(blockers),
        )
