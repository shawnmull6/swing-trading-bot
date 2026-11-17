"""Performance metrics for an offline backtest (Step 16.15, M2-4 Phase 5).

Pure and deterministic: everything is derived from a
:class:`~swing_trading_bot.strategy.backtest.BacktestResult` and the session calendar it
ran on. Nothing is estimated or filled in. A metric that the data cannot support is
``None`` and a :class:`ReportNote` says why (no trades, no losses, an unvalued open
position, a period too short to annualize, a sample below the stated minimum).

**Definitions.**

* *Signals* — accepted setups (``SIGNAL_ACCEPTED``). *Entry orders* — signals that became
  sized DAY limit orders. *Filled* / *unfilled* entries partition the orders that reached
  their session; *blocked* signals never became orders (capacity, stale marks, sizing,
  minimum price, invalid intent).
* *Completed* trades sold every share; *open* trades are marked at the last replayed
  close and are never wins or losses. Win rate, average R, expectancy and profit factor
  use completed trades only. Trade P&L includes fees and dividends.
* *Expectancy* — mean net P&L per completed trade (dollars); ``average_r`` is the mean
  R-multiple (P&L / planned risk at entry).
* *Profit factor* — gross profit / gross loss of completed trades; undefined (``None``)
  without a loss.
* *Total return* — (final equity - initial) / initial, only when every open position is
  valued. *Annualized return* — compounded over the replayed sessions at
  :data:`SESSIONS_PER_YEAR`, only for at least :data:`ANNUALIZE_MIN_SESSIONS` sessions
  and a valued, positive final equity.
* *Max drawdown* — the backtest's peak-to-trough on the daily equity curve.
* *Exposure* — share of sessions holding any position; *average gross exposure* — mean
  of market value / equity.
* *Holding duration* — sessions from the entry session to the exit session (an exit on
  the entry session is 0).
* *Transaction costs* — fees actually charged. Slippage is embedded in fill prices; its
  effect is measured with :func:`cost_sensitivity`, not estimated here.

The sample-size assessment is a guard against reading noise, not a significance test.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, ONE, ZERO
from swing_trading_bot.strategy.backtest import (
    AuditKind,
    BacktestConfig,
    BacktestResult,
    TradeRecord,
    run_backtest,
)
from swing_trading_bot.strategy.market_data import SeriesPreparation, SessionCalendar

SESSIONS_PER_YEAR: Final = 252
ANNUALIZE_MIN_SESSIONS: Final = 252
METRICS_VERSION: Final = "2"
"""Bumped whenever a metric definition changes (part of the frozen research protocol)."""
MIN_COMPLETED_TRADES: Final = 30
"""Below this many completed trades the sample is reported as INSUFFICIENT."""
UNKNOWN_SECTOR: Final = "UNCLASSIFIED"
HUNDRED: Final = Decimal("100")

_UNFILLED: Final = (
    AuditKind.ENTRY_EXPIRED,
    AuditKind.ENTRY_NO_DATA,
    AuditKind.ENTRY_REJECTED_GAP_THROUGH_STOP,
    AuditKind.ENTRY_CANCELLED_SPLIT,
)
_BLOCKED: Final = (
    AuditKind.ENTRY_BLOCKED_STALE_MARK,
    AuditKind.ENTRY_SKIPPED_CAPACITY,
    AuditKind.ENTRY_SKIPPED_MIN_PRICE,
    AuditKind.ENTRY_SIZED_ZERO,
    AuditKind.INTENT_INVALID,
)


class SampleStatus(StrEnum):
    NO_COMPLETED_TRADES = "NO_COMPLETED_TRADES"
    INSUFFICIENT = "INSUFFICIENT"
    SUFFICIENT = "SUFFICIENT"


class ReportNote(StrEnum):
    HYPOTHETICAL_POLICY = "HYPOTHETICAL_POLICY"
    NO_SIGNALS = "NO_SIGNALS"
    NO_TRADES = "NO_TRADES"
    NO_COMPLETED_TRADES = "NO_COMPLETED_TRADES"
    SAMPLE_INSUFFICIENT = "SAMPLE_INSUFFICIENT"
    OPEN_TRADES_AT_END = "OPEN_TRADES_AT_END"
    VALUATION_INCOMPLETE = "VALUATION_INCOMPLETE"
    STALE_FINAL_MARKS = "STALE_FINAL_MARKS"
    PROFIT_FACTOR_UNDEFINED_NO_LOSSES = "PROFIT_FACTOR_UNDEFINED_NO_LOSSES"
    ANNUALIZATION_SKIPPED_SHORT_PERIOD = "ANNUALIZATION_SKIPPED_SHORT_PERIOD"
    ANNUALIZATION_SKIPPED_UNVALUED = "ANNUALIZATION_SKIPPED_UNVALUED"
    DIVIDENDS_EXCLUDED = "DIVIDENDS_EXCLUDED"
    SERIES_REJECTED = "SERIES_REJECTED"
    AMBIGUOUS_INTRABAR_ORDERING = "AMBIGUOUS_INTRABAR_ORDERING"
    NO_SESSIONS = "NO_SESSIONS"


@dataclass(frozen=True, slots=True)
class EndState:
    """Portfolio state after the last replayed session — what an independent fold leaves
    behind and never passes to the next fold."""

    session: date | None
    cash: Decimal
    """Includes unsettled sale proceeds."""
    unsettled_proceeds: Decimal
    dividends_receivable: Decimal
    open_positions: int
    open_market_value: Decimal | None
    """At the last close (None when a final mark is missing under ``UNVALUED``)."""
    pending_orders: int
    equity: Decimal | None
    valuation_complete: bool


@dataclass(frozen=True, slots=True)
class SampleAssessment:
    status: SampleStatus
    completed_trades: int
    minimum: int


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Trades grouped by symbol, sector or exit year. ``net_pnl`` is completed trades'
    realized P&L plus dividends; ``unrealized_pnl`` covers open trades (None when one is
    unvalued)."""

    key: str
    trades: int
    completed: int
    open: int
    wins: int
    losses: int
    net_pnl: Decimal
    unrealized_pnl: Decimal | None
    average_r: Decimal | None


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    label: str
    start_equity: Decimal
    end_equity: Decimal
    return_pct: Decimal


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    policy: str
    executable_today: bool
    disclaimer: str
    period_start: date | None
    period_end: date | None
    sessions: int
    # funnel
    signals: int
    rejection_counts: tuple[tuple[str, int], ...]
    series_rejected: int
    entry_orders: int
    entries_filled: int
    entries_unfilled: tuple[tuple[str, int], ...]
    entries_blocked: tuple[tuple[str, int], ...]
    # trades
    completed_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    average_r: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    # money
    initial_equity: Decimal
    final_equity: Decimal | None
    total_return_pct: Decimal | None
    annualized_return_pct: Decimal | None
    max_drawdown_pct: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    dividend_income: Decimal
    transaction_costs: Decimal
    # exposure
    exposure_pct: Decimal | None
    average_gross_exposure_pct: Decimal | None
    average_holding_sessions: Decimal | None
    max_holding_sessions: int | None
    ambiguous_bars: int
    ambiguous_entries: int
    gap_exits: int
    # breakdowns
    by_symbol: tuple[GroupStats, ...]
    by_sector: tuple[GroupStats, ...]
    by_exit_year: tuple[GroupStats, ...]
    period_returns: tuple[PeriodReturn, ...]
    equity_curve: tuple[tuple[date, Decimal], ...]
    sample: SampleAssessment
    end_state: EndState
    notes: tuple[ReportNote, ...]
    backtest_fingerprint: str

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def _div(a: Decimal, b: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.divide(a, b)


def _net(t: TradeRecord) -> Decimal:
    return t.realized_pnl + t.dividend_income


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return _div(sum(values, ZERO), Decimal(len(values))) if values else None


def _group(key: str, trades: Sequence[TradeRecord]) -> GroupStats:
    done = [t for t in trades if t.completed]
    still = [t for t in trades if not t.completed]
    wins = sum(1 for t in done if _net(t) > ZERO)
    unrealized: Decimal | None = ZERO
    for t in still:
        unrealized = None if unrealized is None or t.unrealized_pnl is None else (
            unrealized + t.unrealized_pnl)  # fmt: skip
    rs = [t.r_multiple for t in done if t.r_multiple is not None]
    return GroupStats(
        key=key, trades=len(trades), completed=len(done), open=len(still), wins=wins,
        losses=len(done) - wins, net_pnl=sum((_net(t) for t in done), ZERO),
        unrealized_pnl=unrealized, average_r=_mean(rs),
    )  # fmt: skip


def _grouped(trades: Iterable[TradeRecord], key: Mapping[str, str] | None,
             by: str) -> tuple[GroupStats, ...]:  # fmt: skip
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        if by == "symbol":
            k = t.symbol
        elif by == "sector":
            k = (key or {}).get(t.symbol, UNKNOWN_SECTOR)
        elif t.completed and t.exit_session is not None:
            k = str(t.exit_session.year)
        else:
            k = "OPEN"
        buckets[k].append(t)
    return tuple(_group(k, buckets[k]) for k in sorted(buckets))


def _period_returns(result: BacktestResult) -> tuple[PeriodReturn, ...]:
    out: list[PeriodReturn] = []
    start = result.summary.initial_equity
    by_year: dict[int, Decimal] = {}
    for point in result.equity_curve:
        by_year[point.session.year] = point.equity
    for year in sorted(by_year):
        end = by_year[year]
        pct = _div(end - start, start) * HUNDRED if start > ZERO else ZERO
        out.append(PeriodReturn(str(year), start, end, pct))
        start = end
    return tuple(out)


def _annualized(result: BacktestResult, sessions: int,
                notes: list[ReportNote]) -> Decimal | None:  # fmt: skip
    final = result.summary.final_equity
    initial = result.summary.initial_equity
    if final is None:
        notes.append(ReportNote.ANNUALIZATION_SKIPPED_UNVALUED)
        return None
    if sessions < ANNUALIZE_MIN_SESSIONS or final <= ZERO:
        notes.append(ReportNote.ANNUALIZATION_SKIPPED_SHORT_PERIOD)
        return None
    growth = _div(final, initial)
    exponent = _div(Decimal(SESSIONS_PER_YEAR), Decimal(sessions))
    return (DECIMAL_CONTEXT.power(growth, exponent) - ONE) * HUNDRED


def _holding(trades: Sequence[TradeRecord], calendar: SessionCalendar) -> list[int]:
    out: list[int] = []
    for t in trades:
        if t.completed and t.exit_session is not None:
            out.append(calendar.index(t.exit_session) - calendar.index(t.entry_session))
    return out


def _count(
    audit_kinds: Counter[AuditKind], kinds: Sequence[AuditKind]
) -> tuple[tuple[str, int], ...]:
    return tuple((k.value, audit_kinds[k]) for k in kinds if audit_kinds[k])


def _notes(
    result: BacktestResult, kinds: Counter[AuditKind], *, done_count: int
) -> list[ReportNote]:
    s = result.summary
    checks = (
        (not result.executable_today, ReportNote.HYPOTHETICAL_POLICY),
        (not result.equity_curve, ReportNote.NO_SESSIONS),
        (kinds[AuditKind.SIGNAL_ACCEPTED] == 0, ReportNote.NO_SIGNALS),
        (not result.trades, ReportNote.NO_TRADES),
        (bool(result.trades) and done_count == 0, ReportNote.NO_COMPLETED_TRADES),
        (s.open_trades > 0, ReportNote.OPEN_TRADES_AT_END),
        (not s.valuation_complete, ReportNote.VALUATION_INCOMPLETE),
        (s.stale_final_marks > 0, ReportNote.STALE_FINAL_MARKS),
        (not s.dividends_included, ReportNote.DIVIDENDS_EXCLUDED),
        (kinds[AuditKind.SERIES_REJECTED] > 0, ReportNote.SERIES_REJECTED),
        (s.ambiguous_bars + s.ambiguous_entries > 0, ReportNote.AMBIGUOUS_INTRABAR_ORDERING),
    )
    return [note for flag, note in checks if flag]


def evaluate(
    result: BacktestResult,
    calendar: SessionCalendar,
    *,
    sectors: Mapping[str, str] | None = None,
    min_completed_trades: int = MIN_COMPLETED_TRADES,
) -> PerformanceReport:
    """Every metric of ``result`` (see the module docstring for definitions)."""
    s = result.summary
    kinds: Counter[AuditKind] = Counter(e.kind for e in result.audit)
    trades = result.trades
    done = [t for t in trades if t.completed]
    nets = [_net(t) for t in done]
    gross_profit = sum((n for n in nets if n > ZERO), ZERO)
    gross_loss = -sum((n for n in nets if n < ZERO), ZERO)
    curve = result.equity_curve
    sessions = len(curve)
    notes = _notes(result, kinds, done_count=len(done))
    profit_factor: Decimal | None = None
    if done and gross_loss > ZERO:
        profit_factor = _div(gross_profit, gross_loss)
    elif done:
        notes.append(ReportNote.PROFIT_FACTOR_UNDEFINED_NO_LOSSES)
    if not done:
        sample = SampleAssessment(SampleStatus.NO_COMPLETED_TRADES, 0, min_completed_trades)
    elif len(done) < min_completed_trades:
        sample = SampleAssessment(SampleStatus.INSUFFICIENT, len(done), min_completed_trades)
        notes.append(ReportNote.SAMPLE_INSUFFICIENT)
    else:
        sample = SampleAssessment(SampleStatus.SUFFICIENT, len(done), min_completed_trades)
    annualized = _annualized(result, sessions, notes) if curve else None
    held = [p for p in curve if p.market_value > ZERO]
    exposures = [_div(p.market_value, p.equity) for p in curve if p.equity > ZERO]
    holding = _holding(trades, calendar)
    avg_exposure = _mean(exposures)
    return PerformanceReport(
        policy=result.policy.value, executable_today=result.executable_today,
        disclaimer=result.disclaimer,
        period_start=curve[0].session if curve else None,
        period_end=curve[-1].session if curve else None, sessions=sessions,
        signals=kinds[AuditKind.SIGNAL_ACCEPTED], rejection_counts=result.rejection_counts,
        series_rejected=kinds[AuditKind.SERIES_REJECTED],
        entry_orders=kinds[AuditKind.ENTRY_ORDER], entries_filled=kinds[AuditKind.ENTRY_FILLED],
        entries_unfilled=_count(kinds, _UNFILLED), entries_blocked=_count(kinds, _BLOCKED),
        completed_trades=s.completed_trades, open_trades=s.open_trades, wins=s.wins,
        losses=s.losses, win_rate=s.win_rate, average_r=s.average_r, expectancy=s.expectancy,
        profit_factor=profit_factor, gross_profit=gross_profit, gross_loss=gross_loss,
        initial_equity=s.initial_equity, final_equity=s.final_equity,
        total_return_pct=s.total_return_pct, annualized_return_pct=annualized,
        max_drawdown_pct=s.max_drawdown_pct, realized_pnl=s.realized_pnl,
        unrealized_pnl=s.unrealized_pnl, dividend_income=s.dividend_income,
        transaction_costs=s.fees_paid,
        exposure_pct=_div(Decimal(len(held)), Decimal(sessions)) * HUNDRED if curve else None,
        average_gross_exposure_pct=avg_exposure * HUNDRED if avg_exposure is not None else None,
        average_holding_sessions=_mean([Decimal(h) for h in holding]),
        max_holding_sessions=max(holding) if holding else None,
        ambiguous_bars=s.ambiguous_bars, ambiguous_entries=s.ambiguous_entries,
        gap_exits=s.gap_exits,
        by_symbol=_grouped(trades, None, "symbol"),
        by_sector=_grouped(trades, sectors, "sector"),
        by_exit_year=_grouped(trades, None, "year"),
        period_returns=_period_returns(result),
        equity_curve=tuple((p.session, p.equity) for p in curve),
        sample=sample, notes=tuple(dict.fromkeys(notes)),
        end_state=EndState(
            session=curve[-1].session if curve else None, cash=s.cash,
            unsettled_proceeds=s.unsettled_proceeds, dividends_receivable=s.dividends_receivable,
            open_positions=s.open_trades, open_market_value=s.open_market_value,
            pending_orders=s.pending_orders_at_end, equity=s.final_equity,
            valuation_complete=s.valuation_complete,
        ),
        backtest_fingerprint=result.result_fingerprint,
    )  # fmt: skip


# ===================================================================== cost sensitivity


@dataclass(frozen=True, slots=True)
class CostScenario:
    label: str
    entry_slippage_bps: Decimal
    stop_slippage_bps: Decimal
    fees: FeeModel | None = None
    """None keeps the configured fee model."""


@dataclass(frozen=True, slots=True)
class CostSensitivityRow:
    label: str
    policy: str
    entry_slippage_bps: Decimal
    stop_slippage_bps: Decimal
    completed_trades: int
    open_trades: int
    expectancy: Decimal | None
    average_r: Decimal | None
    total_return_pct: Decimal | None
    transaction_costs: Decimal
    result_fingerprint: str


def cost_sensitivity(
    preparations: Sequence[SeriesPreparation],
    calendar: SessionCalendar,
    config: BacktestConfig,
    scenarios: Sequence[CostScenario],
) -> tuple[CostSensitivityRow, ...]:
    """Re-run ``config`` under each cost scenario on identical data and parameters, so
    results differ only through costs."""
    rows: list[CostSensitivityRow] = []
    for sc in scenarios:
        ax = replace(config.assumptions, entry_slippage_bps=sc.entry_slippage_bps,
                     stop_slippage_bps=sc.stop_slippage_bps,
                     fees=sc.fees if sc.fees is not None else config.assumptions.fees)  # fmt: skip
        result = run_backtest(preparations, calendar, replace(config, assumptions=ax))
        s = result.summary
        rows.append(CostSensitivityRow(
            label=sc.label, policy=result.policy.value,
            entry_slippage_bps=sc.entry_slippage_bps, stop_slippage_bps=sc.stop_slippage_bps,
            completed_trades=s.completed_trades, open_trades=s.open_trades,
            expectancy=s.expectancy, average_r=s.average_r,
            total_return_pct=s.total_return_pct, transaction_costs=s.fees_paid,
            result_fingerprint=result.result_fingerprint,
        ))  # fmt: skip
    return tuple(rows)


__all__ = [
    "ANNUALIZE_MIN_SESSIONS",
    "METRICS_VERSION",
    "MIN_COMPLETED_TRADES",
    "SESSIONS_PER_YEAR",
    "CostScenario",
    "CostSensitivityRow",
    "EndState",
    "GroupStats",
    "PerformanceReport",
    "PeriodReturn",
    "ReportNote",
    "SampleAssessment",
    "SampleStatus",
    "cost_sensitivity",
    "evaluate",
]
