"""Portfolio state at fold boundaries: independent folds vs continuous evaluation
(Step 16.15A). Positions, pending orders, cash, unsettled proceeds, dividend receivables,
corporate actions and marks. SYNTHETIC data."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.research.dataset import load_dataset
from swing_trading_bot.strategy.backtest import (
    AuditKind,
    BacktestConfig,
    BacktestResult,
    ExecutionAssumptions,
    ExitPolicyMode,
    run_backtest,
)
from swing_trading_bot.strategy.evaluation import evaluate
from swing_trading_bot.strategy.market_data import (
    CashDividend,
    CorporateActions,
    CoverageWindow,
    SessionCalendar,
    SplitEvent,
    prepare_series,
)
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from swing_trading_bot.strategy.walk_forward import (
    DEFAULT_PERIODS,
    DataView,
    FixedDefaultsSelector,
    FoldDesign,
    FoldStatus,
    HoldoutRelease,
    PortfolioMode,
    SealedResearchData,
    UnsupportedEvaluationModeError,
    WalkForwardProtocol,
    build_folds,
    freeze_protocol,
    run_continuous,
    run_holdout,
    run_walk_forward,
)
from tests.support.research_fixture import (
    DATES,
    SESSIONS,
    SYN_END,
    SYN_START,
    FixtureSpec,
    daily_bar,
    default_spec,
    record,
    synthetic_bars,
    write_fixture,
)
from tests.support.strategy_fixture import LIMITS, REAL_FEES, ZERO_FEES

D = Decimal
CAL = SessionCalendar(SESSIONS)
HOLDOUT = DEFAULT_PERIODS.holdout.start
FIRST = ExitPolicyMode.PROTECTION_FIRST
LINKED = ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS
INITIAL = D(5000)


def _protocol(mode: PortfolioMode = PortfolioMode.INDEPENDENT_FOLDS,
              design: FoldDesign | None = None) -> WalkForwardProtocol:  # fmt: skip
    template = BacktestConfig(SupportResistanceParams(), ExitTemplate(), FIRST,
                              ExecutionAssumptions(fees=REAL_FEES), LIMITS, date(2021, 1, 4),
                              date(2021, 1, 4))  # fmt: skip
    return WalkForwardProtocol(DEFAULT_PERIODS, design or FoldDesign(), template,
                               "fixed-defaults-v1", portfolio_mode=mode)  # fmt: skip


def _sealed(
    tmp_path: Path, spec: FixtureSpec | None = None, name: str = "ds"
) -> SealedResearchData:
    write_fixture(tmp_path / name, spec)
    inputs = load_dataset(tmp_path / name).research_inputs()
    return SealedResearchData(inputs, holdout_start=HOLDOUT)


# ===================================================================== independent folds


def test_independent_folds_start_fresh_whatever_the_previous_fold_left(tmp_path: Path) -> None:
    report = run_walk_forward(_sealed(tmp_path), _protocol(), FixedDefaultsSelector())
    folds = report.folds
    assert all(f.status is FoldStatus.EVALUATED for f in folds)
    for prev, cur in itertools.pairwise(folds):
        for attr in ("executable", "hypothetical"):
            before, after = getattr(prev, attr), getattr(cur, attr)
            end = before.end_state
            assert end.session == prev.fold.test_end
            assert end.pending_orders == 0  # no decision on a fold's last session
            assert end.valuation_complete and end.equity is not None
            # whatever was open, unsettled or receivable is NOT carried forward:
            assert after.equity_curve[0] == (cur.fold.test_start, INITIAL)
            assert after.initial_equity == INITIAL
    executable_open = [f.executable.end_state.open_positions for f in folds if f.executable]
    assert any(n > 0 for n in executable_open)  # the boundary really had open positions
    for agg in report.aggregates:  # no chained returns anywhere in the aggregate
        assert not hasattr(agg, "total_return_pct")


def test_end_state_reports_unsettled_proceeds_of_a_last_session_sale() -> None:
    preps = [_prep("AAA", synthetic_bars("AAA"))]
    long_run = _run(preps, LINKED, date(2022, 1, 3), date(2022, 12, 30))
    sells = sorted({f.session for t in long_run.trades for f in t.fills if f.side == "SELL"})
    exit_day = sells[1]
    result = _run(preps, LINKED, date(2022, 1, 3), exit_day)
    s = result.summary
    proceeds = sum((f.qty * f.price - f.fee for t in result.trades for f in t.fills
                    if f.side == "SELL" and f.session == exit_day), D(0))  # fmt: skip
    assert proceeds > 0
    assert s.unsettled_proceeds == proceeds  # settles on the next session, after the fold
    assert s.pending_orders_at_end == 0
    assert s.final_equity == s.cash + (s.open_market_value or 0) + s.dividends_receivable
    _identity(result)
    end = evaluate(result, CAL).end_state
    assert end.unsettled_proceeds == proceeds and end.cash == s.cash
    nxt = CAL.next_session(exit_day)
    assert nxt is not None
    fresh = _run(preps, LINKED, nxt, nxt + timedelta(days=30))
    assert fresh.equity_curve[0].cash == INITIAL  # the next independent fold starts clean


def test_dividend_receivable_at_a_boundary_is_valued_not_carried() -> None:
    ex_days = [d for d in DATES if d.day <= 3 and d.weekday() == 1 and date(2022, 1, 1) <= d]
    divs = [CashDividend("AAA", d, d + timedelta(days=20), D("0.25")) for d in ex_days]
    preps = [_prep("AAA", synthetic_bars("AAA"), dividends=divs)]
    long_run = _run(preps, FIRST, date(2022, 1, 3), date(2023, 12, 29))
    accrued = [e for e in long_run.audit if e.kind is AuditKind.DIVIDEND_ACCRUED]
    assert accrued
    ex_day = accrued[0].session
    result = _run(preps, FIRST, date(2022, 1, 3), ex_day)
    s = result.summary
    assert s.dividends_receivable > 0  # ex-date inside the fold, pay date after it
    assert s.dividend_income == s.dividends_receivable
    _identity(result)
    assert evaluate(result, CAL).end_state.dividends_receivable == s.dividends_receivable


def test_a_split_after_a_fold_end_cannot_change_that_fold(tmp_path: Path) -> None:
    """A real 2:1 split on 2024-06-03 (raw prices halve from that session). Every fold
    ending before it sees identical data: the event is clipped from its view."""
    split_day = date(2024, 6, 3)
    spec = default_spec()
    for sym, bars in list(spec.bars.items()):
        spec.bars[sym] = [b if b.session_date < split_day else daily_bar(
            sym, b.session_date, (b.open / 2, b.high / 2, b.low / 2, b.close / 2))
            for b in bars]  # fmt: skip
        spec.records[sym] = record(sym, splits=[SplitEvent(sym, split_day, 2, 1, "fixture")])
    base = run_walk_forward(_sealed(tmp_path), _protocol(), FixedDefaultsSelector())
    split = run_walk_forward(_sealed(tmp_path, spec, "split"), _protocol(),
                             FixedDefaultsSelector())  # fmt: skip
    assert base.dataset_digest != split.dataset_digest
    for a, b in zip(base.folds[:4], split.folds[:4], strict=True):  # all end before 2024
        assert a.executable is not None and b.executable is not None
        assert a.hypothetical is not None and b.hypothetical is not None
        assert a.executable.backtest_fingerprint == b.executable.backtest_fingerprint
        assert a.hypothetical.backtest_fingerprint == b.hypothetical.backtest_fingerprint
    va, vb = base.folds[4].executable, split.folds[4].executable  # 2024 contains the split
    assert va is not None and vb is not None
    assert va.backtest_fingerprint != vb.backtest_fingerprint


# ===================================================================== continuous


def test_continuous_mode_is_refused_by_the_independent_runner(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    with pytest.raises(UnsupportedEvaluationModeError):
        run_walk_forward(sealed, _protocol(PortfolioMode.CONTINUOUS), FixedDefaultsSelector())
    with pytest.raises(UnsupportedEvaluationModeError):
        run_continuous(sealed, _protocol(), FixedDefaultsSelector())
    release = HoldoutRelease(sealed.dataset_digest, "x", "y", "r", "why", date(2026, 11, 16))
    with pytest.raises(UnsupportedEvaluationModeError):
        run_holdout(sealed, _protocol(PortfolioMode.CONTINUOUS), FixedDefaultsSelector(), release)


def test_continuous_carries_positions_cash_and_receivables_across_boundaries(
    tmp_path: Path,
) -> None:
    sealed = _sealed(tmp_path)
    protocol = _protocol(PortfolioMode.CONTINUOUS)
    report = run_continuous(sealed, protocol, FixedDefaultsSelector())
    folds = build_folds(protocol.periods, protocol.design, sealed.session_dates)
    assert report.start == folds[0].test_start and report.end == folds[-1].test_end
    assert report.protocol_fingerprint == freeze_protocol(sealed, protocol).fingerprint
    assert [b.session for b in report.boundaries_executable] == [f.test_end for f in folds]
    curve = dict(report.executable.equity_curve)
    for b in report.boundaries_executable:
        assert b.equity == curve[b.session] == b.cash + b.market_value + b.dividends_receivable
    carried = [b for b in report.boundaries_executable[:-1] if b.open_trade_ids]
    assert carried  # positions really cross fold boundaries in this mode
    # only the first session starts from the initial capital; boundaries do not reset it
    assert report.executable.equity_curve[0][1] == INITIAL
    starts = {f.test_start for f in folds[1:]}
    assert any(curve[d] != INITIAL for d in starts)
    end = report.executable.end_state
    assert end.pending_orders == 0 and end.valuation_complete
    assert report.hypothetical.policy == "HYPOTHETICAL_LINKED_EXITS"


def test_continuous_capital_reconciles_over_the_whole_span(tmp_path: Path) -> None:
    report = run_continuous(_sealed(tmp_path), _protocol(PortfolioMode.CONTINUOUS),
                            FixedDefaultsSelector())  # fmt: skip
    for rep in (report.executable, report.hypothetical):
        assert rep.final_equity is not None and rep.unrealized_pnl is not None
        assert rep.final_equity - rep.initial_equity == (
            rep.realized_pnl + rep.unrealized_pnl + rep.dividend_income
        )


def test_continuous_is_refused_when_one_replay_cannot_represent_it(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    gaps = _protocol(PortfolioMode.CONTINUOUS, FoldDesign(test_months=3, step_months=6))
    with pytest.raises(UnsupportedEvaluationModeError, match="contiguous"):
        run_continuous(sealed, gaps, FixedDefaultsSelector())

    class PerFold(FixedDefaultsSelector):
        def select(self, training: DataView,
                   defaults: SupportResistanceParams) -> SupportResistanceParams:  # fmt: skip
            if training.until.year >= 2023:
                return replace(defaults, min_reward_risk=D("2.5"))
            return defaults

    with pytest.raises(UnsupportedEvaluationModeError, match="parameters differ"):
        run_continuous(sealed, _protocol(PortfolioMode.CONTINUOUS), PerFold())


# ===================================================================== helpers


def _prep(symbol: str, bars: object, *, dividends: list[CashDividend] | None = None) -> object:
    window = CoverageWindow(SYN_START, SYN_END, "fixture:attested")
    ca = CorporateActions(symbol, (), tuple(dividends or ()), window, window)
    return prepare_series(symbol, bars, calendar=CAL, corporate_actions=ca)  # type: ignore[arg-type]


def _run(preps: list[object], policy: ExitPolicyMode, start: date, end: date) -> BacktestResult:
    cfg = BacktestConfig(SupportResistanceParams(), ExitTemplate(), policy,
                         ExecutionAssumptions(fees=ZERO_FEES), LIMITS, start, end)  # fmt: skip
    return run_backtest(preps, CAL, cfg)  # type: ignore[arg-type]


def _identity(result: BacktestResult) -> None:
    s = result.summary
    assert s.final_equity is not None and s.unrealized_pnl is not None
    assert (
        s.final_equity - s.initial_equity == s.realized_pnl + s.unrealized_pnl + s.dividend_income
    )
