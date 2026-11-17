"""Backtest performance metrics, zero-trade and incomplete-valuation handling, capacity
ranking and transaction-cost sensitivity (Step 16.15, Phases 5-6). SYNTHETIC data."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.domain.money import DECIMAL_CONTEXT
from swing_trading_bot.strategy.backtest import (
    AuditKind,
    BacktestConfig,
    ExecutionAssumptions,
    ExitPolicyMode,
    MissingPricePolicy,
    compare_policies,
    run_backtest,
)
from swing_trading_bot.strategy.evaluation import (
    CostScenario,
    ReportNote,
    SampleStatus,
    cost_sensitivity,
    evaluate,
)
from swing_trading_bot.strategy.market_data import (
    CorporateActions,
    CoverageWindow,
    SeriesPreparation,
    SessionCalendar,
    prepare_series,
)
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from tests.support.research_fixture import (
    DATES,
    SESSIONS,
    SYN_END,
    SYN_START,
    daily_bar,
    synthetic_bars,
)
from tests.support.strategy_fixture import LIMITS, REAL_FEES, ZERO_FEES

D = Decimal
CAL = SessionCalendar(SESSIONS)
SECTORS = {"AAA": "Tech", "BBB": "Energy", "CCC": "Tech", "DDD": "Energy"}
LINKED = ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS
FIRST = ExitPolicyMode.PROTECTION_FIRST


def _prep(symbol: str, bars: object) -> SeriesPreparation:
    window = CoverageWindow(SYN_START, SYN_END, "fixture:attested")
    ca = CorporateActions(symbol, (), (), window, window)
    return prepare_series(symbol, bars, calendar=CAL, corporate_actions=ca)  # type: ignore[arg-type]


def _with_losses(symbol: str) -> list[DailyBar]:
    """The unscaled sawtooth, but every third entry day breaks down through the stop."""
    bars = list(synthetic_bars(symbol))
    for i, b in enumerate(bars):
        if i % 12 == 8 and (i // 12) % 3 == 0:
            bars[i] = daily_bar(symbol, b.session_date, (D("48.90"), D("49.20"), D("46.50"),
                                                         D("47.00")))  # fmt: skip
    return bars


def _preps() -> list[SeriesPreparation]:
    preps = [_prep(s, synthetic_bars(s, scale=D(1) + D(i) / 2, phase=i * 3))
             for i, s in enumerate(("AAA", "BBB", "CCC"))]  # fmt: skip
    return [*preps, _prep("DDD", _with_losses("DDD"))]


def _cfg(policy: ExitPolicyMode = LINKED, start: date = date(2022, 1, 3),
         end: date = date(2023, 6, 30), **ax: object) -> BacktestConfig:  # fmt: skip
    assumptions = ExecutionAssumptions(**{"fees": ZERO_FEES, **ax})  # type: ignore[arg-type]
    return BacktestConfig(SupportResistanceParams(), ExitTemplate(), policy, assumptions, LIMITS,
                          start, end)  # fmt: skip


def test_metrics_are_consistent_with_the_trades() -> None:
    result = run_backtest(_preps(), CAL, _cfg(fees=REAL_FEES))
    rep = evaluate(result, CAL, sectors=SECTORS)
    done = [t for t in result.trades if t.completed]
    assert rep.completed_trades == len(done) > 30
    assert rep.wins + rep.losses == rep.completed_trades
    assert rep.losses > 0 and rep.gross_loss > 0
    nets = [t.realized_pnl + t.dividend_income for t in done]
    assert rep.gross_profit - rep.gross_loss == sum(nets, D(0))
    assert rep.profit_factor == DECIMAL_CONTEXT.divide(rep.gross_profit, rep.gross_loss)
    assert rep.expectancy == DECIMAL_CONTEXT.divide(sum(nets, D(0)), D(len(done)))
    assert rep.sample.status is SampleStatus.SUFFICIENT
    assert sum(g.trades for g in rep.by_symbol) == len(result.trades)
    assert {g.key for g in rep.by_sector} == {"Tech", "Energy"}
    assert sum(g.trades for g in rep.by_sector) == len(result.trades)
    assert rep.entries_filled == len(result.trades)
    assert rep.entry_orders == rep.entries_filled + sum(n for _, n in rep.entries_unfilled)
    assert rep.signals >= rep.entry_orders
    assert rep.transaction_costs == result.summary.fees_paid > 0
    assert rep.exposure_pct is not None and D(0) < rep.exposure_pct <= D(100)
    assert rep.average_holding_sessions is not None and rep.average_holding_sessions >= 0
    assert rep.sessions == len(CAL.between(date(2022, 1, 3), date(2023, 6, 30)))
    assert rep.annualized_return_pct is not None  # > 252 sessions, valued
    assert rep.equity_curve[-1][1] == result.summary.final_equity
    # yearly returns compound to the total return
    growth = D(1)
    for p in rep.period_returns:
        growth *= 1 + p.return_pct / 100
    assert rep.total_return_pct is not None
    assert abs((growth - 1) * 100 - rep.total_return_pct) < D("1e-20")
    assert ReportNote.HYPOTHETICAL_POLICY in rep.notes
    assert rep.fingerprint == evaluate(result, CAL, sectors=SECTORS).fingerprint
    assert ReportNote.AMBIGUOUS_INTRABAR_ORDERING in rep.notes  # the breakdown entry days


def test_profit_factor_is_undefined_without_losses() -> None:
    rep = evaluate(run_backtest(_preps()[:3], CAL, _cfg()), CAL)
    assert rep.completed_trades > 0 and rep.losses == 0
    assert rep.profit_factor is None
    assert ReportNote.PROFIT_FACTOR_UNDEFINED_NO_LOSSES in rep.notes


def test_executable_policy_reports_open_trades_not_wins() -> None:
    executable, hypothetical = compare_policies(_preps()[:3], CAL, _cfg())  # no stop-outs
    rep = evaluate(executable, CAL)
    assert rep.executable_today and ReportNote.HYPOTHETICAL_POLICY not in rep.notes
    assert rep.completed_trades == 0 and rep.open_trades > 0
    assert rep.win_rate is None and rep.average_r is None and rep.expectancy is None
    assert rep.profit_factor is None
    assert rep.sample.status is SampleStatus.NO_COMPLETED_TRADES
    assert {ReportNote.OPEN_TRADES_AT_END, ReportNote.NO_COMPLETED_TRADES} <= set(rep.notes)
    assert rep.unrealized_pnl is not None
    assert evaluate(hypothetical, CAL).completed_trades > 0


def test_zero_signal_period_is_explicit() -> None:
    flat = [daily_bar("FLAT", d, (D(50), D("50.10"), D("49.90"), D(50))) for d in DATES]
    rep = evaluate(run_backtest([_prep("FLAT", flat)], CAL, _cfg()), CAL)
    assert rep.signals == 0 and rep.completed_trades == 0 and rep.open_trades == 0
    assert {ReportNote.NO_SIGNALS, ReportNote.NO_TRADES} <= set(rep.notes)
    assert rep.total_return_pct == 0 and rep.max_drawdown_pct == 0
    assert rep.win_rate is None and rep.profit_factor is None and rep.average_r is None
    assert rep.exposure_pct == 0
    assert rep.rejection_counts  # every rejection is still counted


def test_short_period_is_not_annualized_and_small_samples_are_flagged() -> None:
    rep = evaluate(run_backtest(_preps(), CAL, _cfg(end=date(2022, 3, 31))), CAL)
    assert rep.annualized_return_pct is None
    assert ReportNote.ANNUALIZATION_SKIPPED_SHORT_PERIOD in rep.notes
    assert rep.sample.status is SampleStatus.INSUFFICIENT
    assert ReportNote.SAMPLE_INSUFFICIENT in rep.notes


def test_unvalued_final_position_blocks_return_metrics() -> None:
    preps = _preps()
    end = date(2022, 3, 31)
    holey = [b for b in synthetic_bars("AAA") if b.session_date != end]
    preps[0] = _prep("AAA", holey)
    cfg = _cfg(FIRST, end=end, missing_final_price=MissingPricePolicy.UNVALUED)
    result = run_backtest(preps, CAL, cfg)
    assert any(e.kind is AuditKind.FINAL_MARK_UNVALUED for e in result.audit)
    rep = evaluate(result, CAL)
    assert rep.final_equity is None and rep.total_return_pct is None
    assert rep.annualized_return_pct is None and rep.unrealized_pnl is None
    assert ReportNote.VALUATION_INCOMPLETE in rep.notes


def test_capacity_limits_rank_and_block_signals() -> None:
    limits = LIMITS.model_copy(update={"max_open_positions": 1})
    cfg = replace(_cfg(), risk_limits=limits)
    result = run_backtest(_preps(), CAL, cfg)
    rep = evaluate(result, CAL)
    blocked = dict(rep.entries_blocked)
    assert blocked.get("ENTRY_SKIPPED_CAPACITY", 0) > 0
    open_at_once = max(
        sum(
            1
            for t in result.trades
            if t.entry_session <= p.session
            and (t.exit_session is None or t.exit_session > p.session)
        )
        for p in result.equity_curve
    )
    assert open_at_once <= 1


def test_cost_sensitivity_changes_only_costs() -> None:
    scenarios = [CostScenario("zero", D(0), D(0)), CostScenario("base", D(5), D(10)),
                 CostScenario("stress", D(25), D(50), REAL_FEES)]  # fmt: skip
    rows = cost_sensitivity(_preps(), CAL, _cfg(), scenarios)
    assert [r.label for r in rows] == ["zero", "base", "stress"]
    assert len({r.result_fingerprint for r in rows}) == 3
    exp = [r.expectancy for r in rows]
    assert all(e is not None for e in exp)
    assert exp[0] >= exp[1] >= exp[2]  # type: ignore[operator]
    assert rows[0].transaction_costs == 0 < rows[2].transaction_costs


@pytest.mark.parametrize("policy", [FIRST, LINKED])
def test_reports_are_deterministic(policy: ExitPolicyMode) -> None:
    a = evaluate(run_backtest(_preps(), CAL, _cfg(policy)), CAL, sectors=SECTORS)
    b = evaluate(run_backtest(_preps(), CAL, _cfg(policy)), CAL, sectors=SECTORS)
    assert a == b and a.fingerprint == b.fingerprint
