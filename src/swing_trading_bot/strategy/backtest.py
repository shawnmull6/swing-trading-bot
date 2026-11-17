"""Deterministic offline replay of the support/resistance strategy (Steps 16.14-16.14A).

No I/O, no wall clock, no randomness: the same inputs always give the same result (and
the same ``result_fingerprint``). Every price is an exact Decimal.

**Session order (no look-ahead).** For each session ``S`` of the replay:

1. dividends whose pay date has come are paid (cash);
2. splits effective at ``S`` are applied to open positions (shares x ratio, prices,
   trailing offset, stop and target rescaled; cash in lieu for fractions); an entry
   order for ``S`` on a split session is cancelled (its limit is on the old basis);
3. dividends with ex-date ``S`` accrue to positions held at the close of ``S-1``;
4. entry orders decided after ``S-1`` closed are worked against ``S``'s **raw** bar
   (see *Entry timing*);
5. exits for positions held before ``S`` are evaluated on ``S``'s raw bar;
6. equity is marked at ``S``'s close; runner highs from the completed bar update the
   stop **for the next session**;
7. after ``S`` closes, setups are evaluated on windows ending at ``S`` and accepted
   signals become DAY limit orders for the next session, sized with
   :func:`swing_trading_bot.risk.sizing.size`. New entries are blocked while any held
   symbol has no bar for ``S`` (stale mark: equity unknown, as the runtime gate does).

**Entry timing (Step 16.14A).** ``ExecutionAssumptions.entry_timing``:

* ``SUBMIT_AFTER_OPEN`` (default; matches the runtime, whose gate blocks entries while
  the market is closed and which never uses extended hours). The order does not exist
  before the open, so the opening print is known when it is submitted. **Open at or
  below the planned stop → the order is not submitted** (``ENTRY_REJECTED_GAP_THROUGH_STOP``).
  Otherwise: open ≤ limit → the order is marketable and fills at
  ``min(limit, open x (1 + entry slippage))`` (the first post-open price is approximated
  by the open); open > limit → it rests and fills at the limit only if the low trades
  through it. If a marketable order's session later reaches the stop, the bar cannot
  show whether the stop was breached before the order reached the market (a pre-submit
  check would have rejected it) or after the fill: ``AMBIGUOUS_ENTRY_ORDERING``, and the
  conservative assumption is *filled, then stopped*.
* ``RESTING_BEFORE_OPEN`` — the order was placed before the open and participates in
  the opening auction. It is irrevocable by then, so the open is **not** used to reject
  it: open ≤ limit fills at the open even when the open is at or below the stop
  (``ENTRY_FILLED_THROUGH_STOP``). The real OMS would then refuse the protective stop
  (``STOP_NOT_BELOW_MARK``) and halt ``STOP_BREACHED_UNPROTECTED`` for an operator; the
  replay models an exit at the open less slippage and labels it.

A resting limit above the stop always fills (as price falls through the limit) before
the stop can be reached, so its entry-day stop is sequential, not ambiguous.

**Exit fills (conservative where the bar cannot tell).** Stops trigger when the low
reaches them and fill at the stop (or the open on a gap) less stop slippage, never
below the low; same bar reaches stop and target → **stop first**; no target is credited
on the entry day; breakeven and trailing take effect from the next session; a missing
bar for a held symbol evaluates nothing (``DATA_GAP_HELD``) and the next open is a gap.

**Accounting (Step 16.14A).** Cash (fees, slippage, settlement of sale proceeds on the
calendar's settlement date, cash in lieu, paid dividends), dividends receivable, and
open-position market value are kept apart. A trade is *completed* only when its last
share is sold; open trades carry unrealized P&L and a marked R-multiple and are never
counted as wins or losses. Final marks use the last replayed session's close; a missing
final price follows ``MissingPricePolicy`` (``LAST_AVAILABLE_CLOSE``: flagged stale;
``UNVALUED``: unrealized P&L, market value and equity are reported as unknown). When
valued, ``final_equity - initial_equity == realized + unrealized + dividends``.

**Exit policies.** ``PROTECTION_FIRST`` is what the current OMS can execute (stop for
every share, partial deferred, therefore no breakeven and no trailing).
``HYPOTHETICAL_LINKED_EXITS`` assumes linked stop/target orders the current OMS cannot
execute (ARCHITECTURE §12.43-§12.44) and is reported for research only.

Not modelled: partial fills, intraday paths beyond the rules above, loss-limit lockouts,
borrow, the ``max_position_pct_equity`` drift after entry.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from swing_trading_bot.config.risk_limits import RiskLimits
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.money import (
    CASH_QUANTUM,
    DECIMAL_CONTEXT,
    ONE,
    PENNY,
    PRICE_QUANTUM,
    SUB_PENNY_THRESHOLD,
    ZERO,
    reservation_amount,
)
from swing_trading_bot.domain.trade_plan import (
    TradePlanError,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
    allocate_exit_quantities,
)
from swing_trading_bot.risk.sizing import SizingInputs, size
from swing_trading_bot.strategy.market_data import (
    SeriesPreparation,
    SessionCalendar,
    SplitEvent,
    StrategyBar,
    SymbolSeries,
)
from swing_trading_bot.strategy.signals import (
    ExitTemplate,
    SetupSignal,
    generate_signal,
    to_trade_plan_intent,
)
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams

BPS: Final = Decimal("10000")
HUNDRED: Final = Decimal("100")
COMPLETED: Final = "COMPLETED"
OPEN: Final = "OPEN"


class ExitPolicyMode(StrEnum):
    PROTECTION_FIRST = "PROTECTION_FIRST"
    HYPOTHETICAL_LINKED_EXITS = "HYPOTHETICAL_LINKED_EXITS"

    @property
    def executable_today(self) -> bool:
        return self is ExitPolicyMode.PROTECTION_FIRST

    @property
    def disclaimer(self) -> str:
        if self.executable_today:
            return ("Executable policy: protective stop only; partial profits deferred; "
                    "no breakeven or trailing (they follow a taken partial).")  # fmt: skip
        return ("HYPOTHETICAL: assumes linked stop/target orders (bucket stops + OCO) that "
                "the current OMS cannot execute. Research comparison only.")  # fmt: skip


class EntryTiming(StrEnum):
    SUBMIT_AFTER_OPEN = "SUBMIT_AFTER_OPEN"
    RESTING_BEFORE_OPEN = "RESTING_BEFORE_OPEN"


class MissingPricePolicy(StrEnum):
    LAST_AVAILABLE_CLOSE = "LAST_AVAILABLE_CLOSE"
    UNVALUED = "UNVALUED"


class AuditKind(StrEnum):
    SERIES_REJECTED = "SERIES_REJECTED"
    SERIES_FINDING = "SERIES_FINDING"
    SIGNAL_ACCEPTED = "SIGNAL_ACCEPTED"
    INTENT_INVALID = "INTENT_INVALID"
    ENTRY_SKIPPED_CAPACITY = "ENTRY_SKIPPED_CAPACITY"
    ENTRY_SKIPPED_MIN_PRICE = "ENTRY_SKIPPED_MIN_PRICE"
    ENTRY_SIZED_ZERO = "ENTRY_SIZED_ZERO"
    ENTRY_BLOCKED_STALE_MARK = "ENTRY_BLOCKED_STALE_MARK"
    ENTRY_ORDER = "ENTRY_ORDER"
    ENTRY_REJECTED_GAP_THROUGH_STOP = "ENTRY_REJECTED_GAP_THROUGH_STOP"
    ENTRY_FILLED = "ENTRY_FILLED"
    ENTRY_FILLED_THROUGH_STOP = "ENTRY_FILLED_THROUGH_STOP"
    AMBIGUOUS_ENTRY_ORDERING = "AMBIGUOUS_ENTRY_ORDERING"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    ENTRY_NO_DATA = "ENTRY_NO_DATA"
    ENTRY_CANCELLED_SPLIT = "ENTRY_CANCELLED_SPLIT"
    ENTRY_DAY_STOP = "ENTRY_DAY_STOP"
    STOP_GAP_EXIT = "STOP_GAP_EXIT"
    STOP_EXIT = "STOP_EXIT"
    AMBIGUOUS_BAR_STOP_FIRST = "AMBIGUOUS_BAR_STOP_FIRST"
    TARGET_FILL = "TARGET_FILL"
    TARGET_GAP_FILL = "TARGET_GAP_FILL"
    TARGET_IGNORED_ENTRY_DAY = "TARGET_IGNORED_ENTRY_DAY"
    PARTIAL_DEFERRED = "PARTIAL_DEFERRED"
    STOP_RAISED = "STOP_RAISED"
    DATA_GAP_HELD = "DATA_GAP_HELD"
    STALE_MARK = "STALE_MARK"
    SPLIT_APPLIED = "SPLIT_APPLIED"
    DIVIDEND_ACCRUED = "DIVIDEND_ACCRUED"
    DIVIDEND_PAID = "DIVIDEND_PAID"
    OPEN_AT_END = "OPEN_AT_END"
    FINAL_MARK_STALE = "FINAL_MARK_STALE"
    FINAL_MARK_UNVALUED = "FINAL_MARK_UNVALUED"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    session: date
    symbol: str
    kind: AuditKind
    detail: str
    trade_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    fees: FeeModel
    entry_slippage_bps: Decimal = Decimal("5")
    stop_slippage_bps: Decimal = Decimal("10")
    require_limit_penetration: bool = True
    entry_timing: EntryTiming = EntryTiming.SUBMIT_AFTER_OPEN
    missing_final_price: MissingPricePolicy = MissingPricePolicy.LAST_AVAILABLE_CLOSE

    def __post_init__(self) -> None:
        for name in ("entry_slippage_bps", "stop_slippage_bps"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v < ZERO or v >= BPS:
                raise ValueError(f"{name} must be a Decimal in [0, 10000)")
        if not isinstance(self.entry_timing, EntryTiming):
            raise ValueError("entry_timing must be an EntryTiming")
        if not isinstance(self.missing_final_price, MissingPricePolicy):
            raise ValueError("missing_final_price must be a MissingPricePolicy")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    params: SupportResistanceParams
    exit_template: ExitTemplate
    policy: ExitPolicyMode
    assumptions: ExecutionAssumptions
    risk_limits: RiskLimits
    start: date
    """First decision session."""
    end: date
    """Last session replayed; open positions are marked at its close."""
    initial_equity: Decimal = Decimal("5000")
    capital_ceiling: Decimal = Decimal("5000")

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        if self.initial_equity <= ZERO or self.capital_ceiling <= ZERO:
            raise ValueError("initial_equity and capital_ceiling must be positive")

    def canonical(self) -> dict[str, object]:
        return {
            "params": self.params.canonical(),
            "exit_template": {k: str(v) for k, v in asdict(self.exit_template).items()},
            "policy": self.policy.value,
            "assumptions": {k: str(v) for k, v in asdict(self.assumptions).items()},
            "risk_limits": self.risk_limits.model_dump(mode="json"),
            "start": str(self.start),
            "end": str(self.end),
            "initial_equity": str(self.initial_equity),
            "capital_ceiling": str(self.capital_ceiling),
        }


@dataclass(frozen=True, slots=True)
class Fill:
    session: date
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    reason: AuditKind


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: str
    symbol: str
    signal_ref: str
    decision_session: date
    entry_session: date
    entry_limit: Decimal
    initial_stop: Decimal
    partial_target: Decimal
    reward_risk: Decimal
    entry_qty: Decimal
    entry_price: Decimal
    fills: tuple[Fill, ...]
    status: str
    """``COMPLETED`` (every share sold) or ``OPEN`` (shares held at the end)."""
    exit_session: date | None
    open_qty: Decimal
    realized_pnl: Decimal
    dividend_income: Decimal
    mark_price: Decimal | None
    """Final mark for an OPEN trade (None when completed or unvalued)."""
    mark_session: date | None
    mark_stale: bool
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    """Zero for a completed trade; None for an unvalued open trade."""
    planned_risk: Decimal
    r_multiple: Decimal | None
    """(realized + dividends) / planned risk, for COMPLETED trades only."""
    marked_r_multiple: Decimal | None
    """(realized + dividends + unrealized) / planned risk, for valued OPEN trades only."""
    partial_deferred_touches: int
    split_adjusted: bool
    entry_ordering_ambiguous: bool

    @property
    def completed(self) -> bool:
        return self.status == COMPLETED

    @property
    def total_pnl(self) -> Decimal | None:
        if self.unrealized_pnl is None:
            return None
        return self.realized_pnl + self.dividend_income + self.unrealized_pnl


@dataclass(frozen=True, slots=True)
class EquityPoint:
    session: date
    cash: Decimal
    market_value: Decimal
    """Held shares at the last available close (stale symbols listed)."""
    dividends_receivable: Decimal
    equity: Decimal
    stale_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    initial_equity: Decimal
    cash: Decimal
    open_market_value: Decimal | None
    dividends_receivable: Decimal
    final_equity: Decimal | None
    """cash + open market value + dividends receivable; None when not fully valued."""
    total_return_pct: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    dividend_income: Decimal
    fees_paid: Decimal
    total_trades: int
    completed_trades: int
    open_trades: int
    wins: int
    losses: int
    """Completed trades only; open trades are neither."""
    win_rate: Decimal | None
    average_r: Decimal | None
    expectancy: Decimal | None
    max_drawdown_pct: Decimal
    gap_exits: int
    ambiguous_bars: int
    ambiguous_entries: int
    gap_through_stop_rejections: int
    gap_through_stop_fills: int
    partial_deferred_touches: int
    stale_final_marks: int
    valuation_complete: bool
    unsettled_proceeds: Decimal
    """Sale proceeds in ``cash`` whose settlement date is after the last replayed session
    (counted in equity; not yet available for new buys)."""
    pending_orders_at_end: int
    """Entry orders still unworked after the last session (none: no decision is made on
    the last session, because its next session is outside the replay)."""
    dividends_included: bool
    entry_timing: EntryTiming
    missing_final_price: MissingPricePolicy


@dataclass(frozen=True, slots=True)
class BacktestResult:
    policy: ExitPolicyMode
    executable_today: bool
    disclaimer: str
    config_fingerprint: str
    data_fingerprint: str
    result_fingerprint: str
    trades: tuple[TradeRecord, ...]
    audit: tuple[AuditEvent, ...]
    equity_curve: tuple[EquityPoint, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    summary: BacktestSummary


# ===================================================================== helpers


def _tick(value: Decimal) -> Decimal:
    return PENNY if value >= SUB_PENNY_THRESHOLD else PRICE_QUANTUM


def ceil_tick(value: Decimal) -> Decimal:
    return value.quantize(_tick(value), rounding=ROUND_CEILING, context=DECIMAL_CONTEXT)


def _buy_px(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_CEILING, context=DECIMAL_CONTEXT)


def _sell_px(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)


def _cash(value: Decimal) -> Decimal:
    return value.quantize(CASH_QUANTUM, rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)


def _div(a: Decimal, b: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.divide(a, b)


def _scale_price(value: Decimal, ratio: Fraction) -> Decimal:
    return _div(value * ratio.denominator, Decimal(ratio.numerator))


def _fp(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(slots=True)
class _Pending:
    signal: SetupSignal
    intent: TradePlanIntent
    qty: Decimal
    reservation: Decimal


@dataclass(slots=True)
class _Position:
    trade_id: str
    signal: SetupSignal
    trail: TrailingStopIntent
    entry_session: date
    entry_qty: Decimal
    entry_price: Decimal
    qty: Decimal
    avg_entry: Decimal
    stop: Decimal
    target: Decimal
    partial_remaining: Decimal
    phase: str  # PROTECTED | RUNNER | SMALL
    planned_risk: Decimal
    realized: Decimal
    dividends: Decimal = ZERO
    high_water: Decimal | None = None
    next_stop: Decimal | None = None
    deferred: int = 0
    split_adjusted: bool = False
    entry_ambiguous: bool = False
    fills: list[Fill] = field(default_factory=list)
    exit_session: date | None = None


@dataclass(slots=True)
class _Receivable:
    amount: Decimal
    pay_date: date
    symbol: str
    trade_id: str


# ===================================================================== engine


class _Replay:
    def __init__(self, series: Mapping[str, SymbolSeries], calendar: SessionCalendar,
                 config: BacktestConfig) -> None:  # fmt: skip
        self.series = series
        self.calendar = calendar
        self.cfg = config
        self.ax = config.assumptions
        self.fees = config.assumptions.fees
        self.cash = config.initial_equity
        self.unsettled: list[tuple[Decimal, date]] = []
        self.receivables: list[_Receivable] = []
        self.positions: dict[str, _Position] = {}
        self.pending: dict[str, _Pending] = {}
        self.closed: list[_Position] = []
        self.audit: list[AuditEvent] = []
        self.curve: list[EquityPoint] = []
        self.rejections: Counter[str] = Counter()
        self.fees_paid = ZERO
        self.gap_exits = 0
        self.ambiguous = 0
        self.ambiguous_entries = 0
        self.gap_rejections = 0
        self.gap_fills = 0
        self.trade_seq = 0

    # ------------------------------------------------------------- driver
    def run(self) -> None:
        sessions = self.calendar.between(self.cfg.start, self.cfg.end)
        for s in sessions:
            self._pay_dividends(s)
            self._splits(s)
            self._accrue_dividends(s)
            self._entries(s)
            for sym in sorted(self.positions):
                pos = self.positions[sym]
                if pos.entry_session != s:
                    self._held(pos, s)
            self._mark(s)
            nxt = self.calendar.next_session(s)
            if nxt is not None and nxt <= self.cfg.end:
                self._decide(s)

    def _log(self, s: date, sym: str, kind: AuditKind, detail: str = "",
             trade_id: str | None = None) -> None:  # fmt: skip
        self.audit.append(AuditEvent(s, sym, kind, detail, trade_id))

    # ------------------------------------------------------------- cash
    def _available(self, on: date) -> Decimal:
        unsettled = sum((a for a, d in self.unsettled if d > on), ZERO)
        reserved = sum((p.reservation for p in self.pending.values()), ZERO)
        return self.cash - unsettled - reserved

    def _deployed(self) -> Decimal:
        held = sum((p.qty * p.avg_entry for p in self.positions.values()), ZERO)
        return held + sum((p.reservation for p in self.pending.values()), ZERO)

    def _receivable(self) -> Decimal:
        return sum((r.amount for r in self.receivables), ZERO)

    def _sell(self, pos: _Position, s: date, qty: Decimal, price: Decimal,
              reason: AuditKind) -> None:  # fmt: skip
        fee = self.fees.sell_fee(qty, price)
        proceeds = qty * price - fee
        self.cash += proceeds
        settle = self.calendar.settlement_date(s) or s
        self.unsettled.append((proceeds, settle))
        self.fees_paid += fee
        pos.realized += qty * (price - pos.avg_entry) - fee
        pos.qty -= qty
        pos.fills.append(Fill(s, "SELL", qty, price, fee, reason))
        self._log(s, pos.signal.symbol, reason, f"{qty} @ {price} fee {fee}", pos.trade_id)
        if pos.qty == ZERO:
            pos.exit_session = s
            self.closed.append(self.positions.pop(pos.signal.symbol))

    # ------------------------------------------------------------- dividends
    def _pay_dividends(self, s: date) -> None:
        due = [r for r in self.receivables if r.pay_date <= s]
        self.receivables = [r for r in self.receivables if r.pay_date > s]
        for r in due:
            self.cash += r.amount
            self._log(s, r.symbol, AuditKind.DIVIDEND_PAID, str(r.amount), r.trade_id)

    def _accrue_dividends(self, s: date) -> None:
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            for div in self.series[sym].dividends_on(s):
                amount = _cash(pos.qty * div.amount)
                pos.dividends += amount
                self._log(s, sym, AuditKind.DIVIDEND_ACCRUED,
                          f"{pos.qty} sh x {div.amount} = {amount}, pay {div.pay_date}",
                          pos.trade_id)  # fmt: skip
                if div.pay_date <= s:
                    self.cash += amount
                    self._log(s, sym, AuditKind.DIVIDEND_PAID, str(amount), pos.trade_id)
                else:
                    self.receivables.append(_Receivable(amount, div.pay_date, sym,
                                                        pos.trade_id))  # fmt: skip

    # ------------------------------------------------------------- splits
    def _splits(self, s: date) -> None:
        for sym in sorted(set(self.positions) | set(self.pending)):
            for split in self.series[sym].splits_on(s):
                pending = self.pending.get(sym)
                if pending is not None and pending.signal.entry_session == s:
                    del self.pending[sym]
                    self._log(s, sym, AuditKind.ENTRY_CANCELLED_SPLIT,
                              f"{split.new_shares}:{split.old_shares}")  # fmt: skip
                if sym in self.positions:
                    self._split_position(self.positions[sym], split, s)

    def _split_position(self, pos: _Position, split: SplitEvent, s: date) -> None:
        ratio = split.ratio
        exact = _div(pos.qty * ratio.numerator, Decimal(ratio.denominator))
        whole = exact.to_integral_value(rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)
        fraction = exact - whole
        prev = self._prior_bar(pos.signal.symbol, s)
        avg_post = _scale_price(pos.avg_entry, ratio)
        in_lieu_px = _scale_price(prev.close, ratio) if prev is not None else avg_post
        pos.avg_entry = avg_post
        pos.stop = ceil_tick(_scale_price(pos.stop, ratio))
        pos.target = ceil_tick(_scale_price(pos.target, ratio))
        if pos.next_stop is not None:
            pos.next_stop = ceil_tick(_scale_price(pos.next_stop, ratio))
        if pos.high_water is not None:
            pos.high_water = _scale_price(pos.high_water, ratio)
        if pos.trail.mode is TrailMode.ABSOLUTE:  # a dollar offset is on the old basis
            offset = _scale_price(pos.trail.offset, ratio).quantize(
                PENNY, rounding=ROUND_CEILING, context=DECIMAL_CONTEXT
            )
            pos.trail = TrailingStopIntent(TrailMode.ABSOLUTE, max(offset, PENNY))
        scaled_partial = _div(pos.partial_remaining * ratio.numerator, Decimal(ratio.denominator))
        pos.partial_remaining = min(
            whole, scaled_partial.to_integral_value(rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)
        )
        pos.qty = whole
        pos.split_adjusted = True
        detail = (f"{split.new_shares}:{split.old_shares} qty {whole} stop {pos.stop} "
                  f"target {pos.target}")  # fmt: skip
        if fraction > ZERO:
            cash = _cash(fraction * in_lieu_px)
            self.cash += cash
            pos.realized += cash - fraction * avg_post
            detail += f" cash-in-lieu {fraction} sh = {cash}"
        self._log(s, pos.signal.symbol, AuditKind.SPLIT_APPLIED, detail, pos.trade_id)
        if pos.qty == ZERO:
            pos.exit_session = s
            self.closed.append(self.positions.pop(pos.signal.symbol))

    def _prior_bar(self, sym: str, s: date) -> StrategyBar | None:
        i = self.calendar.index(s)
        if i == 0:
            return None
        return self.series[sym].last_bar_on_or_before(self.calendar.dates[i - 1])

    # ------------------------------------------------------------- entries
    def _entries(self, s: date) -> None:
        after_open = self.ax.entry_timing is EntryTiming.SUBMIT_AFTER_OPEN
        for sym in sorted(self.pending):
            order = self.pending.pop(sym)
            sig = order.signal
            stop = sig.protective_stop
            bar = self.series[sym].bar(s)
            if bar is None:
                self._log(s, sym, AuditKind.ENTRY_NO_DATA, sig.signal_ref)
                continue
            if after_open and bar.open <= stop:
                self.gap_rejections += 1
                self._log(s, sym, AuditKind.ENTRY_REJECTED_GAP_THROUGH_STOP,
                          f"open {bar.open} <= stop {stop}: order not submitted")  # fmt: skip
                continue
            price = self._entry_price(bar, sig.entry_limit)
            if price is None:
                self._log(s, sym, AuditKind.ENTRY_EXPIRED,
                          f"limit {sig.entry_limit} low {bar.low}")  # fmt: skip
                continue
            pos = self._open(order, price, s)
            self._entry_day(pos, bar, s, marketable=bar.open <= sig.entry_limit)

    def _entry_price(self, bar: StrategyBar, limit: Decimal) -> Decimal | None:
        slip = ONE + _div(self.ax.entry_slippage_bps, BPS)
        if bar.open <= limit:
            return min(limit, bar.high, _buy_px(bar.open * slip))
        through = bar.low < limit if self.ax.require_limit_penetration else bar.low <= limit
        return limit if through else None

    def _open(self, order: _Pending, price: Decimal, s: date) -> _Position:
        sig, qty = order.signal, order.qty
        fee = self.fees.buy_fee(qty)
        self.cash -= qty * price + fee
        self.fees_paid += fee
        self.trade_seq += 1
        alloc = allocate_exit_quantities(qty, order.intent.allocation)
        pos = _Position(
            trade_id=f"bt-{self.trade_seq:05d}-{sig.symbol}", signal=sig,
            trail=order.intent.trailing, entry_session=s, entry_qty=qty, entry_price=price,
            qty=qty, avg_entry=price, stop=sig.protective_stop, target=sig.partial_target,
            partial_remaining=alloc.partial_qty, phase="PROTECTED" if alloc.feasible else "SMALL",
            planned_risk=max(ZERO, price - sig.protective_stop) * qty, realized=-fee,
        )  # fmt: skip
        pos.fills.append(Fill(s, "BUY", qty, price, fee, AuditKind.ENTRY_FILLED))
        self.positions[sig.symbol] = pos
        self._log(s, sig.symbol, AuditKind.ENTRY_FILLED, f"{qty} @ {price} fee {fee}",
                  pos.trade_id)  # fmt: skip
        return pos

    def _entry_day(self, pos: _Position, bar: StrategyBar, s: date, *, marketable: bool) -> None:
        sym = pos.signal.symbol
        if bar.open <= pos.stop:  # only a RESTING_BEFORE_OPEN order can get here
            self.gap_fills += 1
            self._log(s, sym, AuditKind.ENTRY_FILLED_THROUGH_STOP,
                      f"open {bar.open} <= stop {pos.stop}: resting order filled in the "
                      "opening auction; the OMS would refuse the stop and halt "
                      "STOP_BREACHED_UNPROTECTED; modelled as an exit at the open",
                      pos.trade_id)  # fmt: skip
            self._sell(pos, s, pos.qty, self._stop_px(bar.open, bar), AuditKind.ENTRY_DAY_STOP)
            return
        if bar.low <= pos.stop:
            if self.ax.entry_timing is EntryTiming.SUBMIT_AFTER_OPEN and marketable:
                self.ambiguous_entries += 1
                pos.entry_ambiguous = True
                self._log(s, sym, AuditKind.AMBIGUOUS_ENTRY_ORDERING,
                          f"low {bar.low} <= stop {pos.stop}: breach before submission "
                          "(order rejected) or after the fill cannot be told from the bar; "
                          "assumed filled, then stopped", pos.trade_id)  # fmt: skip
            self._sell(pos, s, pos.qty, self._stop_px(pos.stop, bar), AuditKind.ENTRY_DAY_STOP)
            return
        if self._target_touched(pos, bar):
            self._log(s, sym, AuditKind.TARGET_IGNORED_ENTRY_DAY,
                      f"high {bar.high} target {pos.target}", pos.trade_id)  # fmt: skip

    # ------------------------------------------------------------- exits
    def _stop_px(self, base: Decimal, bar: StrategyBar) -> Decimal:
        slip = ONE - _div(self.ax.stop_slippage_bps, BPS)
        return max(bar.low, _sell_px(base * slip))

    def _target_touched(self, pos: _Position, bar: StrategyBar) -> bool:
        if pos.phase != "PROTECTED" or pos.partial_remaining == ZERO:
            return False
        if self.ax.require_limit_penetration:
            return bar.high > pos.target
        return bar.high >= pos.target

    def _held(self, pos: _Position, s: date) -> None:
        sym = pos.signal.symbol
        if pos.next_stop is not None:
            if pos.next_stop > pos.stop:
                self._log(s, sym, AuditKind.STOP_RAISED, f"{pos.stop} -> {pos.next_stop}",
                          pos.trade_id)  # fmt: skip
                pos.stop = pos.next_stop
            pos.next_stop = None
        bar = self.series[sym].bar(s)
        if bar is None:
            self._log(s, sym, AuditKind.DATA_GAP_HELD, "no bar; stop not evaluated", pos.trade_id)
            return
        linked = self.cfg.policy is ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS
        if bar.open <= pos.stop:
            self.gap_exits += 1
            self._sell(pos, s, pos.qty, self._stop_px(bar.open, bar), AuditKind.STOP_GAP_EXIT)
            return
        touched = self._target_touched(pos, bar)
        if touched and not linked:
            pos.deferred += 1
            self._log(s, sym, AuditKind.PARTIAL_DEFERRED, f"high {bar.high} target {pos.target}",
                      pos.trade_id)  # fmt: skip
            touched = False
        if touched and bar.open >= pos.target:
            self._take_partial(pos, s, _sell_px(bar.open), AuditKind.TARGET_GAP_FILL)
            if pos.qty > ZERO and bar.low <= pos.stop:  # open came first: sequential
                self._sell(pos, s, pos.qty, self._stop_px(pos.stop, bar), AuditKind.STOP_EXIT)
                return
        elif bar.low <= pos.stop:
            if touched:
                self.ambiguous += 1
                self._log(s, sym, AuditKind.AMBIGUOUS_BAR_STOP_FIRST,
                          f"low {bar.low} <= stop {pos.stop}; high {bar.high} > target "
                          f"{pos.target}", pos.trade_id)  # fmt: skip
            self._sell(pos, s, pos.qty, self._stop_px(pos.stop, bar), AuditKind.STOP_EXIT)
            return
        elif touched:
            self._take_partial(pos, s, pos.target, AuditKind.TARGET_FILL)
        if sym in self.positions:
            self._trail(pos, bar)

    def _take_partial(self, pos: _Position, s: date, price: Decimal, kind: AuditKind) -> None:
        qty = pos.partial_remaining
        pos.partial_remaining = ZERO
        self._sell(pos, s, qty, price, kind)
        if pos.qty > ZERO:
            pos.phase = "RUNNER"
            if self.cfg.exit_template.breakeven_after_partial:
                be = pos.avg_entry.quantize(PENNY, rounding=ROUND_CEILING,
                                            context=DECIMAL_CONTEXT)  # fmt: skip
                pos.next_stop = max(pos.stop, be, pos.next_stop or ZERO)

    def _trail(self, pos: _Position, bar: StrategyBar) -> None:
        if pos.phase != "RUNNER":
            return
        pos.high_water = bar.high if pos.high_water is None else max(pos.high_water, bar.high)
        trailed = pos.trail.stop_for(pos.high_water)
        if trailed is not None and trailed > max(pos.stop, pos.next_stop or ZERO):
            pos.next_stop = trailed

    # ------------------------------------------------------------- marks
    def _mark(self, s: date) -> None:
        value = ZERO
        stale: list[str] = []
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            bar = self.series[sym].last_bar_on_or_before(s)
            price = pos.avg_entry if bar is None else bar.close
            if bar is None or bar.session_date != s:
                stale.append(sym)
                seen = bar.session_date if bar else None
                self._log(s, sym, AuditKind.STALE_MARK, f"last bar {seen}", pos.trade_id)
            value += pos.qty * price
        receivable = self._receivable()
        self.curve.append(EquityPoint(s, self.cash, value, receivable,
                                      self.cash + value + receivable, tuple(stale)))  # fmt: skip

    # ------------------------------------------------------------- decisions
    def _decide(self, s: date) -> None:
        candidates: list[SetupSignal] = []
        for sym in sorted(self.series):
            if sym in self.positions or sym in self.pending:
                continue
            result = generate_signal(self.series[sym], self.calendar, s, self.cfg.params)
            if result.signal is None:
                if result.window_issue is not None:
                    self.rejections[f"WINDOW:{result.window_issue.value}"] += 1
                else:
                    self.rejections.update(r.value for r in result.reasons)
                continue
            candidates.append(result.signal)
        candidates.sort(key=lambda c: (-c.reward_risk, c.symbol))
        stale = self.curve[-1].stale_symbols
        for sig in candidates:
            self._log(s, sig.symbol, AuditKind.SIGNAL_ACCEPTED,
                      f"{sig.signal_ref} entry {sig.entry_limit} stop {sig.protective_stop} "
                      f"target {sig.partial_target} rr {sig.reward_risk:.4f}")  # fmt: skip
            if stale:
                self._log(s, sig.symbol, AuditKind.ENTRY_BLOCKED_STALE_MARK,
                          f"equity unknown: no {s} bar for {','.join(stale)}")  # fmt: skip
                continue
            self._order(sig, s)

    def _order(self, sig: SetupSignal, s: date) -> None:
        limits = self.cfg.risk_limits
        try:
            intent = to_trade_plan_intent(sig, self.cfg.exit_template)
        except TradePlanError as exc:
            self._log(s, sig.symbol, AuditKind.INTENT_INVALID, str(exc))
            return
        if len(self.positions) + len(self.pending) >= limits.max_open_positions:
            self._log(s, sig.symbol, AuditKind.ENTRY_SKIPPED_CAPACITY,
                      f"max_open_positions {limits.max_open_positions}")  # fmt: skip
            return
        if sig.entry_limit < limits.min_price:
            self._log(s, sig.symbol, AuditKind.ENTRY_SKIPPED_MIN_PRICE, str(limits.min_price))
            return
        point = self.curve[-1]
        stressed = point.market_value + sum((p.reservation for p in self.pending.values()), ZERO)
        available = self._available(sig.entry_session)
        preview = size(
            SizingInputs(
                entry=sig.entry_limit, stop=sig.protective_stop, equity=point.equity,
                baseline_equity=point.equity, held_market_value=ZERO,
                pending_symbol_commitment=ZERO,
                capital_headroom=self.cfg.capital_ceiling - self._deployed(),
                available_cash=available,
                portfolio_stressed=stressed * limits.gap_stress.scenario_gap_pct,
                broker_cash=available,
            ),
            limits, self.fees,
        )  # fmt: skip
        binding = ",".join(c.value for c in preview.binding)
        if preview.max_qty < 1:
            self._log(s, sig.symbol, AuditKind.ENTRY_SIZED_ZERO, f"binding {binding}")
            return
        qty = Decimal(preview.max_qty)
        reservation = reservation_amount(qty * sig.entry_limit + self.fees.buy_fee(qty))
        self.pending[sig.symbol] = _Pending(sig, intent, qty, reservation)
        self._log(s, sig.symbol, AuditKind.ENTRY_ORDER,
                  f"BUY {qty} LIMIT {sig.entry_limit} DAY {sig.entry_session} "
                  f"{self.ax.entry_timing.value} binding {binding}")  # fmt: skip


# ===================================================================== results


@dataclass(frozen=True, slots=True)
class _FinalMark:
    price: Decimal | None
    session: date | None
    stale: bool


def _final_mark(pos: _Position, last: date, series: SymbolSeries,
                policy: MissingPricePolicy) -> _FinalMark:  # fmt: skip
    bar = series.last_bar_on_or_before(last)
    if bar is not None and bar.session_date == last:
        return _FinalMark(bar.close, last, stale=False)
    if policy is MissingPricePolicy.UNVALUED or bar is None:
        return _FinalMark(None, None, stale=True)
    return _FinalMark(bar.close, bar.session_date, stale=True)


def _record(pos: _Position, mark: _FinalMark | None) -> TradeRecord:
    sig = pos.signal
    earned = pos.realized + pos.dividends
    completed = pos.qty == ZERO
    unrealized: Decimal | None = ZERO
    market_value: Decimal | None = None
    r: Decimal | None = None
    marked_r: Decimal | None = None
    if completed:
        if pos.planned_risk > ZERO:
            r = _div(earned, pos.planned_risk)
    elif mark is not None and mark.price is not None:
        market_value = pos.qty * mark.price
        unrealized = pos.qty * (mark.price - pos.avg_entry)
        if pos.planned_risk > ZERO:
            marked_r = _div(earned + unrealized, pos.planned_risk)
    else:
        unrealized = None
    return TradeRecord(
        trade_id=pos.trade_id, symbol=sig.symbol, signal_ref=sig.signal_ref,
        decision_session=sig.decision_session, entry_session=pos.entry_session,
        entry_limit=sig.entry_limit, initial_stop=sig.protective_stop,
        partial_target=sig.partial_target, reward_risk=sig.reward_risk,
        entry_qty=pos.entry_qty, entry_price=pos.entry_price, fills=tuple(pos.fills),
        status=COMPLETED if completed else OPEN, exit_session=pos.exit_session,
        open_qty=pos.qty, realized_pnl=pos.realized, dividend_income=pos.dividends,
        mark_price=None if completed or mark is None else mark.price,
        mark_session=None if completed or mark is None else mark.session,
        mark_stale=False if completed or mark is None else mark.stale,
        market_value=market_value, unrealized_pnl=unrealized, planned_risk=pos.planned_risk,
        r_multiple=r, marked_r_multiple=marked_r,
        partial_deferred_touches=pos.deferred, split_adjusted=pos.split_adjusted,
        entry_ordering_ambiguous=pos.entry_ambiguous,
    )  # fmt: skip


def _max_drawdown(curve: Sequence[EquityPoint]) -> Decimal:
    peak: Decimal | None = None
    worst = ZERO
    for point in curve:
        peak = point.equity if peak is None else max(peak, point.equity)
        if peak > ZERO:
            worst = max(worst, _div(peak - point.equity, peak))
    return worst * HUNDRED


def _summary(
    rp: _Replay, trades: Sequence[TradeRecord], *, dividends_included: bool, last: date
) -> BacktestSummary:
    initial = rp.cfg.initial_equity
    done = [t for t in trades if t.completed]
    still = [t for t in trades if not t.completed]
    earned = [t.realized_pnl + t.dividend_income for t in done]
    wins = sum(1 for e in earned if e > ZERO)
    rs = [t.r_multiple for t in done if t.r_multiple is not None]
    valued = all(t.market_value is not None for t in still)
    mv = sum((t.market_value or ZERO for t in still), ZERO) if valued else None
    unrealized = sum((t.unrealized_pnl or ZERO for t in still), ZERO) if valued else None
    receivable = rp._receivable()
    final = rp.cash + mv + receivable if mv is not None else None
    n = Decimal(len(done))
    return BacktestSummary(
        initial_equity=initial, cash=rp.cash, open_market_value=mv,
        dividends_receivable=receivable, final_equity=final,
        total_return_pct=_div(final - initial, initial) * HUNDRED if final is not None else None,
        realized_pnl=sum((t.realized_pnl for t in trades), ZERO),
        unrealized_pnl=unrealized,
        dividend_income=sum((t.dividend_income for t in trades), ZERO),
        fees_paid=rp.fees_paid, total_trades=len(trades), completed_trades=len(done),
        open_trades=len(still), wins=wins, losses=len(done) - wins,
        win_rate=_div(Decimal(wins), n) if done else None,
        average_r=_div(sum(rs, ZERO), Decimal(len(rs))) if rs else None,
        expectancy=_div(sum(earned, ZERO), n) if done else None,
        max_drawdown_pct=_max_drawdown(rp.curve), gap_exits=rp.gap_exits,
        ambiguous_bars=rp.ambiguous, ambiguous_entries=rp.ambiguous_entries,
        gap_through_stop_rejections=rp.gap_rejections, gap_through_stop_fills=rp.gap_fills,
        partial_deferred_touches=sum(t.partial_deferred_touches for t in trades),
        stale_final_marks=sum(1 for t in still if t.mark_stale),
        unsettled_proceeds=sum((a for a, d in rp.unsettled if d > last), ZERO),
        pending_orders_at_end=len(rp.pending),
        valuation_complete=valued, dividends_included=dividends_included,
        entry_timing=rp.ax.entry_timing, missing_final_price=rp.ax.missing_final_price,
    )  # fmt: skip


def run_backtest(
    preparations: Iterable[SeriesPreparation],
    calendar: SessionCalendar,
    config: BacktestConfig,
) -> BacktestResult:
    """Replay ``config`` over the accepted series. Rejected series are audited and
    excluded; nothing about them is guessed."""
    preps = sorted(preparations, key=lambda p: p.symbol)
    symbols = [p.symbol for p in preps]
    if len(set(symbols)) != len(symbols):
        raise ValueError("one preparation per symbol")
    series = {p.symbol: p.series for p in preps if p.series is not None}
    rp = _Replay(series, calendar, config)
    sessions = calendar.between(config.start, config.end)
    first_day = sessions[0] if sessions else config.start
    last = sessions[-1] if sessions else config.end
    for p in preps:
        if p.series is None:
            issues = ",".join(sorted({f.issue.value for f in p.findings}))
            rp._log(first_day, p.symbol, AuditKind.SERIES_REJECTED, issues)
        for f in p.findings if p.series is not None else ():
            rp._log(first_day, p.symbol, AuditKind.SERIES_FINDING, f"{f.issue.value} {f.detail}")
    rp.run()
    records: list[TradeRecord] = [_record(pos, None) for pos in rp.closed]
    for sym in sorted(rp.positions):
        pos = rp.positions[sym]
        mark = _final_mark(pos, last, series[sym], config.assumptions.missing_final_price)
        rp._log(last, sym, AuditKind.OPEN_AT_END, f"qty {pos.qty}", pos.trade_id)
        if mark.price is None:
            rp._log(last, sym, AuditKind.FINAL_MARK_UNVALUED, f"no {last} close", pos.trade_id)
        elif mark.stale:
            rp._log(last, sym, AuditKind.FINAL_MARK_STALE,
                    f"marked at {mark.session} close {mark.price}", pos.trade_id)  # fmt: skip
        records.append(_record(pos, mark))
    trades = sorted(records, key=lambda t: t.trade_id)
    dividends_included = all(s.dividends_included for s in series.values())
    summary = _summary(rp, trades, dividends_included=dividends_included, last=last)
    config_fp = _fp(config.canonical())
    data_fp = _fp([[p.symbol, p.series.provenance.digest if p.series else None,
                    [asdict(f) for f in p.findings]] for p in preps]
                  + [[str(d) for d in calendar.dates]])  # fmt: skip
    body = {
        "trades": [asdict(t) for t in trades],
        "audit": [asdict(a) for a in rp.audit],
        "curve": [asdict(c) for c in rp.curve],
        "rejections": sorted(rp.rejections.items()),
        "summary": asdict(summary),
    }
    return BacktestResult(
        policy=config.policy, executable_today=config.policy.executable_today,
        disclaimer=config.policy.disclaimer, config_fingerprint=config_fp,
        data_fingerprint=data_fp, result_fingerprint=_fp([config_fp, data_fp, body]),
        trades=tuple(trades), audit=tuple(rp.audit), equity_curve=tuple(rp.curve),
        rejection_counts=tuple(sorted(rp.rejections.items())), summary=summary,
    )  # fmt: skip


def compare_policies(
    preparations: Sequence[SeriesPreparation],
    calendar: SessionCalendar,
    config: BacktestConfig,
) -> tuple[BacktestResult, BacktestResult]:
    """(executable ``PROTECTION_FIRST``, ``HYPOTHETICAL_LINKED_EXITS``) on identical data."""
    executable = run_backtest(preparations, calendar,
                              replace(config, policy=ExitPolicyMode.PROTECTION_FIRST))  # fmt: skip
    hypothetical = run_backtest(
        preparations, calendar, replace(config, policy=ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS)
    )
    return executable, hypothetical


__all__ = [
    "AuditEvent",
    "AuditKind",
    "BacktestConfig",
    "BacktestResult",
    "BacktestSummary",
    "EntryTiming",
    "EquityPoint",
    "ExecutionAssumptions",
    "ExitPolicyMode",
    "Fill",
    "MissingPricePolicy",
    "TradeRecord",
    "ceil_tick",
    "compare_policies",
    "run_backtest",
]
