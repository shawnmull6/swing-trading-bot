"""Offline replay of the support/resistance strategy (Steps 16.14 and 16.14A)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from swing_trading_bot.domain.money import DECIMAL_CONTEXT
from swing_trading_bot.risk.sizing import SizingInputs, size
from swing_trading_bot.strategy.backtest import (
    AuditKind,
    BacktestConfig,
    BacktestResult,
    EntryTiming,
    ExecutionAssumptions,
    ExitPolicyMode,
    MissingPricePolicy,
    compare_policies,
    run_backtest,
)
from swing_trading_bot.strategy.market_data import (
    CashDividend,
    DividendPolicy,
    SeriesPreparation,
    SplitEvent,
    prepare_series,
)
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from tests.support.strategy_fixture import (
    CALENDAR,
    LIMITS,
    OHLC,
    REAL_FEES,
    ZERO_FEES,
    Scenario,
    attested,
    bar,
    prepared,
    setup_scenario,
)

D = Decimal
LINKED = ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS
FIRST = ExitPolicyMode.PROTECTION_FIRST
AFTER_OPEN = EntryTiming.SUBMIT_AFTER_OPEN
RESTING = EntryTiming.RESTING_BEFORE_OPEN
STOP = D("47.07")
LIMIT = D("49.00")
ENTRY_FILL = D("48.9245")  # min(49.00, 48.90 x 1.0005) rounded up to 0.0001
DAY1: OHLC = (D("48.90"), D("49.80"), D("48.70"), D("49.60"))
WINNER: list[OHLC] = [
    DAY1,
    (D("49.60"), D("52.00"), D("49.40"), D("51.80")),
    (D("51.80"), D("56.50"), D("51.60"), D("56.30")),  # through the 56.18 target
    (D("56.30"), D("58.00"), D("55.00"), D("57.50")),
    (D("57.50"), D("57.60"), D("53.00"), D("53.50")),  # trailing stop 53.77 hit
]


def _ax(**kw: object) -> ExecutionAssumptions:
    return ExecutionAssumptions(**{"fees": ZERO_FEES, **kw})  # type: ignore[arg-type]


def _cfg(sc: Scenario, policy: ExitPolicyMode = LINKED,
         assumptions: ExecutionAssumptions | None = None) -> BacktestConfig:  # fmt: skip
    return BacktestConfig(SupportResistanceParams(), ExitTemplate(), policy,
                          assumptions or _ax(), LIMITS, sc.decision_session,
                          sc.days[-1])  # fmt: skip


def _run(
    future: Sequence[OHLC],
    policy: ExitPolicyMode = LINKED,
    *,
    skip: Sequence[date] = (),
    splits_at: dict[int, tuple[int, int]] | None = None,
    dividends_at: dict[int, tuple[int, Decimal]] | None = None,
    assumptions: ExecutionAssumptions | None = None,
    dividend_policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE,
) -> tuple[Scenario, BacktestResult]:
    """``splits_at``/``dividends_at`` are keyed by sessions after the decision (1 = entry
    session); a dividend is (pay offset, amount)."""
    sc = setup_scenario(future=list(future))
    splits = [SplitEvent(sc.symbol, _day(sc, k), n, o) for k, (n, o) in (splits_at or {}).items()]
    divs = [CashDividend(sc.symbol, _day(sc, k),
                         CALENDAR.dates[CALENDAR.index(_day(sc, k)) + pay], amount)
            for k, (pay, amount) in (dividends_at or {}).items()]  # fmt: skip
    prep = prepared(sc, skip=skip, splits=splits, dividends=divs, dividend_policy=dividend_policy)
    return sc, run_backtest([prep], CALENDAR, _cfg(sc, policy, assumptions))


def _kinds(result: BacktestResult) -> list[AuditKind]:
    return [a.kind for a in result.audit]


def _day(sc: Scenario, k: int) -> date:
    return sc.days[sc.decision_index + k]


def _assert_consistent(res: BacktestResult) -> None:
    """Cash, positions, receivables and equity agree; P&L explains the change."""
    s = res.summary
    assert s.final_equity is not None and s.open_market_value is not None
    assert s.unrealized_pnl is not None
    assert s.final_equity == s.cash + s.open_market_value + s.dividends_receivable
    assert s.final_equity - s.initial_equity == (s.realized_pnl + s.unrealized_pnl
                                                 + s.dividend_income)  # fmt: skip
    assert s.open_market_value == sum((t.market_value or D(0) for t in res.trades), D(0))
    assert s.completed_trades + s.open_trades == s.total_trades == len(res.trades)
    assert s.wins + s.losses == s.completed_trades
    last = res.equity_curve[-1]
    if not last.stale_symbols:
        assert last.equity == s.final_equity and last.cash == s.cash


# --------------------------------------------------------------------- lifecycle
def test_hypothetical_linked_exits_take_partial_then_trail_the_runner() -> None:
    sc, res = _run(WINNER)
    assert not res.executable_today and "HYPOTHETICAL" in res.disclaimer
    (t,) = res.trades
    assert (t.entry_qty, t.entry_price, t.status) == (D(12), ENTRY_FILL, "COMPLETED")
    sells = [(f.session, f.qty, f.price, f.reason) for f in t.fills if f.side == "SELL"]
    assert sells == [
        (_day(sc, 3), D(6), D("56.18"), AuditKind.TARGET_FILL),
        (_day(sc, 5), D(6), D("53.7162"), AuditKind.STOP_EXIT),  # 53.77 x 0.999, >= low
    ]
    raised = [a.detail for a in res.audit if a.kind is AuditKind.STOP_RAISED]
    # breakeven (48.93) is superseded by the trail 56.50 - 4.23 = 52.27 the next session
    assert raised == ["47.07 -> 52.27", "52.27 -> 53.77"]
    expected = 6 * (D("56.18") - ENTRY_FILL) + 6 * (D("53.7162") - ENTRY_FILL)
    assert t.realized_pnl == expected == res.summary.realized_pnl
    assert res.summary.final_equity == D(5000) + expected
    assert t.r_multiple == DECIMAL_CONTEXT.divide(expected, (ENTRY_FILL - STOP) * 12)
    assert t.marked_r_multiple is None and t.unrealized_pnl == 0 and t.mark_price is None
    assert (res.summary.wins, res.summary.losses, res.summary.open_trades) == (1, 0, 0)
    _assert_consistent(res)


def test_protection_first_defers_partials_and_keeps_the_initial_stop() -> None:
    _, res = _run(WINNER, FIRST)
    assert res.executable_today and "deferred" in res.disclaimer
    (t,) = res.trades
    assert t.status == "OPEN" and not [f for f in t.fills if f.side == "SELL"]
    assert t.partial_deferred_touches == 3 == res.summary.partial_deferred_touches
    assert AuditKind.TARGET_FILL not in _kinds(res) and AuditKind.STOP_RAISED not in _kinds(res)
    assert _kinds(res)[-1] is AuditKind.OPEN_AT_END


def test_compare_policies_runs_both_on_identical_data() -> None:
    sc = setup_scenario(future=WINNER)
    executable, hypothetical = compare_policies([prepared(sc)], CALENDAR, _cfg(sc))
    assert (executable.policy, hypothetical.policy) == (FIRST, LINKED)
    assert executable.data_fingerprint == hypothetical.data_fingerprint
    assert executable.config_fingerprint != hypothetical.config_fingerprint
    assert executable.summary.final_equity != hypothetical.summary.final_equity


def test_size_comes_from_the_shared_risk_sizing_contract() -> None:
    _, res = _run(WINNER)
    preview = size(SizingInputs(entry=LIMIT, stop=STOP, equity=D(5000),
                                baseline_equity=D(5000), held_market_value=D(0),
                                pending_symbol_commitment=D(0), capital_headroom=D(5000),
                                available_cash=D(5000), portfolio_stressed=D(0),
                                broker_cash=D(5000)), LIMITS, ZERO_FEES)  # fmt: skip
    assert res.trades[0].entry_qty == D(preview.max_qty) == D(12)
    order = next(a for a in res.audit if a.kind is AuditKind.ENTRY_ORDER)
    assert "binding RISK_BUDGET" in order.detail and "SUBMIT_AFTER_OPEN" in order.detail


# --------------------------------------------------------------------- entry timing / gaps
@pytest.mark.parametrize(
    "first_bar",
    [
        (D("46.50"), D("46.80"), D("46.00"), D("46.40")),  # gap below the stop
        (STOP, D("48.00"), D("46.90"), D("47.50")),  # open exactly at the stop
    ],
)
def test_entry_is_not_submitted_when_the_open_is_at_or_below_the_stop(first_bar: OHLC) -> None:
    _, res = _run([first_bar, DAY1])
    assert res.trades == ()
    rejection = next(a for a in res.audit
                     if a.kind is AuditKind.ENTRY_REJECTED_GAP_THROUGH_STOP)  # fmt: skip
    assert f"<= stop {STOP}: order not submitted" in rejection.detail
    assert res.summary.gap_through_stop_rejections == 1
    assert res.summary.cash == res.summary.final_equity == D(5000)  # nothing was bought


def test_gap_above_the_stop_enters_normally() -> None:
    _, res = _run([(D("47.08"), D("48.00"), D("47.08"), D("47.90"))], FIRST)
    (t,) = res.trades
    assert t.entry_price == D("47.1036")  # 47.08 x 1.0005, below the 49.00 limit
    assert AuditKind.ENTRY_REJECTED_GAP_THROUGH_STOP not in _kinds(res)
    assert t.status == "OPEN" and not t.entry_ordering_ambiguous


def test_limit_already_resting_before_the_open_cannot_be_withdrawn() -> None:
    gap = (D("46.50"), D("46.80"), D("46.00"), D("46.40"))
    _, res = _run([gap], assumptions=_ax(entry_timing=RESTING))
    (t,) = res.trades
    assert t.entry_price == D("46.5233")  # filled in the opening auction
    assert [f.reason for f in t.fills] == [AuditKind.ENTRY_FILLED, AuditKind.ENTRY_DAY_STOP]
    assert t.fills[-1].price == D("46.4535")  # exit at the open less slippage
    assert AuditKind.ENTRY_FILLED_THROUGH_STOP in _kinds(res)
    assert AuditKind.ENTRY_REJECTED_GAP_THROUGH_STOP not in _kinds(res)
    assert res.summary.gap_through_stop_fills == 1
    detail = next(a.detail for a in res.audit if a.kind is AuditKind.ENTRY_FILLED_THROUGH_STOP)
    assert "STOP_BREACHED_UNPROTECTED" in detail
    _assert_consistent(res)


def test_marketable_entry_with_a_later_stop_breach_is_ambiguous_and_conservative() -> None:
    falls: OHLC = (D("48.90"), D("49.10"), D("46.90"), D("47.20"))
    _, res = _run([falls])
    (t,) = res.trades
    assert t.entry_ordering_ambiguous and res.summary.ambiguous_entries == 1
    assert AuditKind.AMBIGUOUS_ENTRY_ORDERING in _kinds(res)
    # conservative: filled at the open, then stopped at the stop less slippage
    assert [f.reason for f in t.fills] == [AuditKind.ENTRY_FILLED, AuditKind.ENTRY_DAY_STOP]
    assert (t.fills[0].price, t.fills[-1].price) == (ENTRY_FILL, D("47.0229"))
    assert t.r_multiple is not None and t.r_multiple < 0 and res.summary.losses == 1


def test_resting_order_breach_after_the_fill_is_sequential_not_ambiguous() -> None:
    falls: OHLC = (D("48.90"), D("49.10"), D("46.90"), D("47.20"))
    _, res = _run([falls], assumptions=_ax(entry_timing=RESTING))
    assert AuditKind.AMBIGUOUS_ENTRY_ORDERING not in _kinds(res)
    assert res.trades[0].fills[-1].reason is AuditKind.ENTRY_DAY_STOP
    # a limit above the open rests: price must cross 49.00 before 47.07, so fill comes first
    through: OHLC = (D("49.50"), D("49.60"), D("46.90"), D("47.20"))
    _, res2 = _run([through])
    assert AuditKind.AMBIGUOUS_ENTRY_ORDERING not in _kinds(res2)
    assert res2.trades[0].entry_price == LIMIT


def test_overnight_gap_through_the_stop_fills_at_the_open() -> None:
    gap: OHLC = (D("46.00"), D("46.50"), D("45.50"), D("46.20"))
    _, res = _run([DAY1, gap])
    (t,) = res.trades
    exit_fill = t.fills[-1]
    assert exit_fill.reason is AuditKind.STOP_GAP_EXIT
    assert exit_fill.price == D("45.954")  # 46.00 x 0.999, not the 47.07 stop
    assert t.r_multiple is not None and t.r_multiple < -1  # loss beyond 1R
    assert res.summary.gap_exits == 1


def test_ambiguous_bar_assumes_stop_first() -> None:
    both: OHLC = (D("49.60"), D("56.50"), D("47.00"), D("50.00"))
    _, res = _run([DAY1, both])
    (t,) = res.trades
    assert [f.reason for f in t.fills[1:]] == [AuditKind.STOP_EXIT]
    assert t.fills[-1].price == D("47.0229")  # 47.07 x 0.999
    assert AuditKind.AMBIGUOUS_BAR_STOP_FIRST in _kinds(res)
    assert res.summary.ambiguous_bars == 1


def test_gap_above_target_fills_partial_at_the_open() -> None:
    up: OHLC = (D("57.00"), D("57.50"), D("56.80"), D("57.20"))
    _, res = _run([DAY1, up])
    partial = res.trades[0].fills[1]
    assert (partial.reason, partial.price, partial.qty) == (AuditKind.TARGET_GAP_FILL,
                                                           D("57.00"), D(6))  # fmt: skip


def test_no_target_credit_on_the_entry_day() -> None:
    _, res = _run([(D("48.90"), D("57.00"), D("48.80"), D("56.00"))])
    assert AuditKind.TARGET_IGNORED_ENTRY_DAY in _kinds(res)
    assert len(res.trades[0].fills) == 1


@pytest.mark.parametrize(("penetration", "filled"), [(True, False), (False, True)])
def test_a_touch_of_the_limit_is_not_a_fill_by_default(penetration: bool, filled: bool) -> None:
    touch: OHLC = (D("49.50"), D("50.00"), LIMIT, D("49.80"))
    _, res = _run([touch], assumptions=_ax(require_limit_penetration=penetration))
    assert bool(res.trades) is filled
    if filled:
        assert res.trades[0].entry_price == LIMIT
    else:
        assert AuditKind.ENTRY_EXPIRED in _kinds(res)


# --------------------------------------------------------------------- open-position accounting
def test_open_gain_is_not_a_completed_win() -> None:
    _, res = _run(WINNER, FIRST)
    (t,) = res.trades
    s = res.summary
    assert t.status == "OPEN" and t.unrealized_pnl is not None and t.unrealized_pnl > 0
    assert t.r_multiple is None and t.marked_r_multiple is not None
    assert (s.completed_trades, s.open_trades, s.wins, s.losses) == (0, 1, 0, 0)
    assert s.win_rate is None and s.average_r is None and s.expectancy is None
    assert (t.mark_price, t.mark_session, t.mark_stale) == (D("53.50"), res.equity_curve[-1]
                                                            .session, False)  # fmt: skip
    assert t.market_value == 12 * D("53.50")
    assert t.unrealized_pnl == 12 * (D("53.50") - ENTRY_FILL)
    assert s.cash == D(5000) - 12 * ENTRY_FILL
    assert s.open_market_value == t.market_value
    assert s.realized_pnl == 0
    _assert_consistent(res)


@pytest.mark.parametrize("policy", [FIRST, LINKED])
@pytest.mark.parametrize("fees", [ZERO_FEES, REAL_FEES])
def test_cash_positions_and_equity_reconcile(policy: ExitPolicyMode, fees: object) -> None:
    _, res = _run(WINNER, policy, assumptions=_ax(fees=fees),
                  dividends_at={2: (3, D("0.25"))})  # fmt: skip
    _assert_consistent(res)
    for point in res.equity_curve:
        assert point.equity == point.cash + point.market_value + point.dividends_receivable


def test_missing_final_price_last_available_close_is_flagged_stale() -> None:
    sc = setup_scenario(future=WINNER)
    _, res = _run(WINNER, FIRST, skip=[_day(sc, 5)])
    (t,) = res.trades
    assert t.mark_stale and t.mark_session == _day(sc, 4) and t.mark_price == D("57.50")
    assert res.summary.stale_final_marks == 1 and res.summary.valuation_complete
    assert AuditKind.FINAL_MARK_STALE in _kinds(res)
    assert res.equity_curve[-1].stale_symbols == ("SRX",)
    _assert_consistent(res)


def test_missing_final_price_unvalued_fails_closed() -> None:
    sc = setup_scenario(future=WINNER)
    _, res = _run(WINNER, FIRST, skip=[_day(sc, 5)],
                  assumptions=_ax(missing_final_price=MissingPricePolicy.UNVALUED))  # fmt: skip
    (t,) = res.trades
    s = res.summary
    assert t.mark_price is None and t.unrealized_pnl is None and t.market_value is None
    assert t.total_pnl is None and t.marked_r_multiple is None
    assert not s.valuation_complete
    assert s.final_equity is None and s.open_market_value is None and s.unrealized_pnl is None
    assert s.total_return_pct is None
    assert s.cash == D(5000) - 12 * ENTRY_FILL  # cash is still known exactly
    assert AuditKind.FINAL_MARK_UNVALUED in _kinds(res)


# --------------------------------------------------------------------- missing data
def test_missing_session_while_held_blocks_entries_and_is_never_filled_in() -> None:
    path = [DAY1, (D("49"), D("50"), D("48"), D("49")),
            (D("46.00"), D("46.50"), D("45.50"), D("46.20"))]  # fmt: skip
    sc = setup_scenario(future=path)
    _, res = _run(path, skip=[_day(sc, 2)])
    kinds = _kinds(res)
    assert AuditKind.DATA_GAP_HELD in kinds and AuditKind.STALE_MARK in kinds
    assert res.trades[0].fills[-1].reason is AuditKind.STOP_GAP_EXIT
    gap_day = next(a.session for a in res.audit if a.kind is AuditKind.DATA_GAP_HELD)
    assert gap_day == _day(sc, 2)


def test_stale_mark_blocks_new_entries() -> None:
    held = setup_scenario("AAA", future=WINNER)
    late = setup_scenario("BBB", future=WINNER[:2], start=held.days[2])
    # BBB decides two sessions after AAA (AAA is held by then); AAA has no bar that
    # session -> equity unknown
    preps = [prepared(held, skip=[late.decision_session]), prepared(late)]
    res = run_backtest(preps, CALENDAR, _cfg(held))
    blocked = [a for a in res.audit if a.kind is AuditKind.ENTRY_BLOCKED_STALE_MARK]
    assert [(a.symbol, a.session) for a in blocked] == [("BBB", late.decision_session)]
    assert {t.symbol for t in res.trades} == {"AAA"}


def test_missing_entry_session_expires_the_order() -> None:
    sc = setup_scenario(future=[DAY1, DAY1])
    _, res = _run([DAY1, DAY1], skip=[_day(sc, 1)])
    assert AuditKind.ENTRY_NO_DATA in _kinds(res) and res.trades == ()


def test_rejected_series_is_audited_and_excluded() -> None:
    sc = setup_scenario(future=WINNER)
    bad_bars = [replace(b, feed="iex") for b in setup_scenario("BAD", future=WINNER).bars()]
    bad = prepare_series("BAD", bad_bars, calendar=CALENDAR, corporate_actions=attested("BAD"))
    missing = prepare_series("NOCA", setup_scenario("NOCA", future=WINNER).bars(),
                             calendar=CALENDAR, corporate_actions=None)  # fmt: skip
    res = run_backtest([prepared(sc), bad, missing], CALENDAR, _cfg(sc))
    rejected = [(a.symbol, a.detail) for a in res.audit if a.kind is AuditKind.SERIES_REJECTED]
    assert rejected == [("BAD", "WRONG_FEED"), ("NOCA", "CORPORATE_ACTIONS_MISSING")]
    assert {t.symbol for t in res.trades} == {"SRX"}


# --------------------------------------------------------------------- corporate actions
def _halved(rows: Sequence[OHLC]) -> list[OHLC]:
    return [tuple(x / 2 for x in r) for r in rows]  # type: ignore[misc]


def test_split_while_held_preserves_economics() -> None:
    path = [DAY1, (D("49.60"), D("52.00"), D("49.40"), D("51.80")),
            (D("51.80"), D("53.00"), D("51.00"), D("52.40"))]  # fmt: skip
    _, plain = _run(path, FIRST)
    split_path = [path[0], *_halved(path[1:])]
    _, split = _run(split_path, FIRST, splits_at={2: (2, 1)})
    (t,) = split.trades
    assert t.split_adjusted and t.open_qty == D(24)
    detail = next(a.detail for a in split.audit if a.kind is AuditKind.SPLIT_APPLIED)
    assert detail == "2:1 qty 24 stop 23.54 target 28.09"  # stop rounded UP to the cent
    assert t.total_pnl == plain.trades[0].total_pnl
    assert split.summary.final_equity == plain.summary.final_equity
    _assert_consistent(split)


def test_split_with_fractional_shares_pays_cash_in_lieu() -> None:
    path = [DAY1, (D("49.60"), D("52.00"), D("49.40"), D("51.80"))]
    scaled = [path[0], tuple(x * 5 / 7 for x in path[1])]
    _, res = _run(scaled, FIRST, splits_at={2: (7, 5)})  # type: ignore[arg-type]
    detail = next(a.detail for a in res.audit if a.kind is AuditKind.SPLIT_APPLIED)
    assert detail.startswith("7:5 qty 16 ") and "cash-in-lieu 0.8 sh" in detail
    in_lieu = D(detail.rsplit("= ", 1)[1])
    assert in_lieu == (D("0.8") * DAY1[3] * 5 / 7).quantize(D("0.0001"), rounding="ROUND_FLOOR")
    assert res.summary.cash == D(5000) - 12 * ENTRY_FILL + in_lieu
    assert res.trades[0].fills[0].qty == D(12)
    s = res.summary
    assert s.final_equity is not None and s.unrealized_pnl is not None
    # exact up to the 34-digit division of the 7:5 average cost
    gap = s.final_equity - s.initial_equity - (s.realized_pnl + s.unrealized_pnl)
    assert abs(gap) < D("1e-20")


def test_split_on_the_entry_session_cancels_the_order() -> None:
    _, res = _run([_halved([DAY1])[0]], splits_at={1: (2, 1)})
    assert AuditKind.ENTRY_CANCELLED_SPLIT in _kinds(res) and res.trades == ()


def test_dividend_accrues_on_ex_date_and_is_paid_on_pay_date() -> None:
    sc, res = _run(WINNER, FIRST, dividends_at={2: (2, D("0.25"))})
    (t,) = res.trades
    assert t.dividend_income == 12 * D("0.25") == res.summary.dividend_income
    accrued = next(a for a in res.audit if a.kind is AuditKind.DIVIDEND_ACCRUED)
    paid = next(a for a in res.audit if a.kind is AuditKind.DIVIDEND_PAID)
    assert (accrued.session, paid.session) == (_day(sc, 2), _day(sc, 4))
    by_day = {p.session: p for p in res.equity_curve}
    assert by_day[_day(sc, 2)].dividends_receivable == D("3.00")
    assert by_day[_day(sc, 4)].dividends_receivable == 0
    assert by_day[_day(sc, 4)].cash == by_day[_day(sc, 3)].cash + D("3.00")
    assert res.summary.dividends_included
    _assert_consistent(res)


def test_dividend_entitlement_requires_holding_before_the_ex_date() -> None:
    # ex-date on the entry session: bought on the ex-date, so not entitled
    _, res = _run(WINNER, FIRST, dividends_at={1: (1, D("0.25"))})
    assert res.trades[0].dividend_income == 0
    assert AuditKind.DIVIDEND_ACCRUED not in _kinds(res)


def test_dividend_receivable_outstanding_at_the_end_counts_in_equity() -> None:
    _, res = _run(WINNER, FIRST, dividends_at={4: (10, D("0.50"))})
    s = res.summary
    assert s.dividends_receivable == D("6.00") and AuditKind.DIVIDEND_PAID not in _kinds(res)
    _assert_consistent(res)


def test_completed_trade_includes_dividends_in_its_result() -> None:
    _, res = _run(WINNER, dividends_at={2: (1, D("0.25"))})
    (t,) = res.trades
    assert t.completed and t.dividend_income == D("3.00")
    assert t.r_multiple == DECIMAL_CONTEXT.divide(t.realized_pnl + D("3.00"), t.planned_risk)


def test_price_return_only_is_explicit_and_labelled() -> None:
    _, res = _run(WINNER, FIRST, dividends_at={2: (2, D("0.25"))},
                  dividend_policy=DividendPolicy.PRICE_RETURN_ONLY)  # fmt: skip
    assert not res.summary.dividends_included and res.summary.dividend_income == 0
    finding = next(a.detail for a in res.audit if a.kind is AuditKind.SERIES_FINDING)
    assert finding.startswith("DIVIDENDS_EXCLUDED price-return only; 1 event(s) ignored")


def test_unexplained_drop_while_held_is_booked_as_a_real_gap() -> None:
    crash: OHLC = (D("24.80"), D("25.00"), D("24.00"), D("24.20"))  # 49.60 -> 24.80 (2:1)
    sc, res = _run([DAY1, crash], FIRST)
    assert res.trades[0].fills[-1].reason is AuditKind.STOP_GAP_EXIT
    finding = next(a.detail for a in res.audit if a.kind is AuditKind.SERIES_FINDING)
    assert finding.startswith(f"PRICE_DISCONTINUITY_UNEXPLAINED {_day(sc, 2)}")


# --------------------------------------------------------------------- costs
def test_fees_and_slippage_reduce_results() -> None:
    _, free = _run(WINNER)
    _, costly = _run(WINNER, assumptions=_ax(fees=REAL_FEES))
    assert costly.summary.fees_paid > 0
    sells = [f for f in costly.trades[0].fills if f.side == "SELL"]
    assert all(f.fee == REAL_FEES.sell_fee(f.qty, f.price) for f in sells)
    assert costly.summary.realized_pnl == free.summary.realized_pnl - costly.summary.fees_paid
    _, no_slip = _run(WINNER, assumptions=_ax(entry_slippage_bps=D(0), stop_slippage_bps=D(0)))
    assert no_slip.trades[0].entry_price == D("48.90")
    assert no_slip.summary.realized_pnl > free.summary.realized_pnl


def test_assumption_and_config_validation() -> None:
    with pytest.raises(ValueError, match="slippage"):
        _ax(stop_slippage_bps=D(-1))
    with pytest.raises(ValueError, match="entry_timing"):
        _ax(entry_timing="SOON")
    sc = setup_scenario()
    with pytest.raises(ValueError, match="end"):
        replace(_cfg(sc), end=date(2026, 1, 5))


# --------------------------------------------------------------------- portfolio limits
def test_max_open_positions_limits_new_entries() -> None:
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    scs = [setup_scenario(n, future=WINNER) for n in names]
    res = run_backtest([prepared(s) for s in scs], CALENDAR, _cfg(scs[0]))
    filled = sorted({t.symbol for t in res.trades})
    assert filled == names[: LIMITS.max_open_positions]
    skipped = [a.symbol for a in res.audit if a.kind is AuditKind.ENTRY_SKIPPED_CAPACITY]
    assert skipped == ["EEE"]
    _assert_consistent(res)


# --------------------------------------------------------------------- reproducibility
def _preps() -> list[SeriesPreparation]:
    out = []
    for n in ("AAA", "BBB", "CCC"):
        sc = setup_scenario(n, future=[DAY1, *_halved(WINNER[1:])])
        div = CashDividend(n, _day(sc, 3), _day(sc, 4), D("0.10"))
        out.append(prepared(sc, splits=[SplitEvent(n, _day(sc, 2), 2, 1)], dividends=[div]))
    return out


def test_replay_with_corporate_actions_is_deterministic_and_order_independent() -> None:
    cfg = _cfg(setup_scenario(future=WINNER))
    first = run_backtest(_preps(), CALENDAR, cfg)
    second = run_backtest(list(reversed(_preps())), CALENDAR, cfg)
    assert first == second
    assert first.result_fingerprint == second.result_fingerprint
    assert len(first.result_fingerprint) == 64
    assert AuditKind.SPLIT_APPLIED in _kinds(first) and AuditKind.DIVIDEND_PAID in _kinds(first)


def test_fingerprints_change_with_inputs() -> None:
    cfg = _cfg(setup_scenario(future=WINNER))
    base = run_backtest(_preps(), CALENDAR, cfg)
    other = run_backtest(_preps(), CALENDAR, replace(cfg, initial_equity=D(6000)))
    assert base.result_fingerprint != other.result_fingerprint
    assert base.data_fingerprint == other.data_fingerprint
    preps = _preps()
    sc = setup_scenario("AAA", future=[DAY1, *_halved(WINNER[1:])])
    preps[0] = prepared(sc, splits=[SplitEvent("AAA", _day(sc, 2), 2, 1)])  # dividend dropped
    assert run_backtest(preps, CALENDAR, cfg).data_fingerprint != base.data_fingerprint


def test_future_data_beyond_the_replay_end_changes_nothing() -> None:
    sc_short = setup_scenario(future=WINNER[:3])
    sc_long = setup_scenario(future=[*WINNER, (D("53.50"), D("54.00"), D("10.00"), D("10.50"))])
    cfg = _cfg(sc_short)
    a = run_backtest([prepared(sc_short)], CALENDAR, cfg)
    b = run_backtest([prepared(sc_long)], CALENDAR, cfg)
    assert (a.trades, a.audit, a.equity_curve) == (b.trades, b.audit, b.equity_curve)


def test_equity_curve_covers_every_replayed_session() -> None:
    sc, res = _run(WINNER)
    assert [p.session for p in res.equity_curve] == list(
        CALENDAR.between(sc.decision_session, sc.days[-1])
    )
    assert res.summary.max_drawdown_pct >= 0


def test_series_bar_helper_rejects_runaway_scenarios() -> None:
    with pytest.raises(ValueError, match="calendar"):
        setup_scenario(start=date(2026, 11, 2))
    assert bar("X", date(2026, 3, 2), DAY1).feed == "sip"
