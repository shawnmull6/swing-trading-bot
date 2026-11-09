"""Step 7: equity baselines, daily/weekly loss limits, gaps and gap stress."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import ReconciliationResult, RuntimeMode, Side
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limit_store import (
    BaselineBasis,
    CloseSource,
    LossEventKind,
    LossLimitEventsRepository,
    SessionClose,
    SnapshotKind,
)
from swing_trading_bot.ledger.loss_limits import LossBlocker, LossLimitMonitor, LossLimitReport
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    IngestResult,
    ReconciliationRunsRepository,
    ResumeOutcome,
    TradingStateRepository,
)
from tests.support.loss_limits import CONFIG, close, et, evaluate_at, hold, monitor
from tests.support.portfolio import CONFIG as PF_CONFIG
from tests.support.portfolio import fill, make_order, new_ledger, reserve

D = Decimal
MON, TUE, WED, THU, FRI = (date(2026, 11, d) for d in (9, 10, 11, 12, 13))
NEXT_MON = date(2026, 11, 16)
Setup = tuple[Database, VirtualPortfolio, LossLimitMonitor]


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    db, pf = new_ledger(tmp_path / "ll.sqlite3")  # created 2026-10-10
    yield db, pf, monitor(db, pf)
    db.close()


@pytest.fixture
def holding(setup: Setup) -> Setup:
    """20 AAPL bought at 100.00 on Monday 10:00 ET; cash 3000."""
    db, pf, _ = setup
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    return setup


def halt_reasons(db: Database) -> list[str]:
    with db.read() as s:
        return [e.reason_code for e in TradingStateRepository().events(s) if e.action == "HALT"]


def events(db: Database, kind: LossEventKind) -> list[date]:
    with db.read() as s:
        return [e.applies_to for e in LossLimitEventsRepository().list(s, 1, kind)]


# ----------------------------------------------------------------- thresholds


def test_initial_portfolio_thresholds(setup: Setup) -> None:
    db, pf, mon = setup
    r = evaluate_at(db, pf, mon, et(MON, 10), {})
    assert r.current_session == MON and r.week_start == MON
    assert r.daily_baseline is not None and r.weekly_baseline is not None
    assert r.daily_baseline.reference_session == date(2026, 11, 6)
    assert r.daily_baseline.basis is BaselineBasis.SESSION_CLOSE
    assert (r.daily_baseline.equity, r.weekly_baseline.equity) == (D(5000), D(5000))
    assert r.daily is not None and r.weekly is not None
    assert (r.daily.budget, r.daily.remaining) == (D("100.00"), D("100.00"))
    assert (r.weekly.budget, r.weekly.remaining) == (D("250.00"), D("250.00"))
    assert r.blockers == ()
    assert not r.blocks_new_entries
    assert LossLimitReport.RISK_REDUCING_EXITS_PERMITTED


def test_inception_baseline_when_portfolio_is_newer_than_reference_close(tmp_path: Path) -> None:
    db, pf = new_ledger(tmp_path / "new.sqlite3", created_at=et(MON, 8))
    r = evaluate_at(db, pf, monitor(db, pf), et(MON, 10), {})
    assert r.daily_baseline is not None
    assert r.daily_baseline.basis is BaselineBasis.INCEPTION
    assert r.daily_baseline.reference_session is None
    assert r.daily_baseline.equity == D(5000)
    db.close()


def test_realized_and_unrealized_pnl_drive_equity(holding: Setup) -> None:
    db, pf, mon = holding
    r = evaluate_at(db, pf, mon, et(MON, 11), {"AAPL": "97.00"})
    assert r.equity == D("4940.00")  # 3000 + 20 x 97
    assert r.daily is not None and (r.daily.loss, r.daily.remaining) == (D("60.00"), D("40.00"))
    assert not r.lockout_active
    order = make_order(db, qty="10", limit="95.00", side=Side.SELL)
    pf.process_fill(fill(order, "10", "95.00", at=et(MON, 12)), now=et(MON, 12))  # -50 realized
    r = evaluate_at(db, pf, mon, et(MON, 12, 1), {"AAPL": "95.00"})
    assert r.equity == D("4900.00")  # cash 3950 + 10 x 95
    assert r.daily is not None and r.daily.loss == D("100.00") and r.daily.breached
    assert r.lockout_active and LossBlocker.DAILY_LOSS_LOCKOUT in r.blockers
    assert events(db, LossEventKind.DAILY_LOCKOUT) == [MON]
    assert "DAILY_LOSS_REPEAT" not in halt_reasons(db)  # a single lockout is not a halt


# ------------------------------------------------- daily lockout & reset


def test_intraday_breach_and_next_session_reset(holding: Setup) -> None:
    db, pf, mon = holding
    r = evaluate_at(db, pf, mon, et(MON, 14), {"AAPL": "94.00"})  # loss 120
    assert r.lockout_active and r.lockout_session == MON
    r = evaluate_at(db, pf, mon, et(MON, 15), {"AAPL": "99.50"})  # recovered intraday
    assert r.lockout_active  # lockout lasts for the rest of the session
    # Next session without Monday's official close: no new baseline -> still locked out.
    r = evaluate_at(db, pf, mon, et(TUE, 10), {"AAPL": "99.50"})
    assert LossBlocker.SESSION_BASELINE_MISSING in r.blockers
    assert r.lockout_active
    assert any("no official close for AAPL on 2026-11-09" in p for p in r.problems)
    close(mon, "AAPL", MON, "99.50")
    r = evaluate_at(db, pf, mon, et(TUE, 10, 5), {"AAPL": "99.50"})
    assert r.daily_baseline is not None and r.daily_baseline.equity == D("4990.00")
    assert not r.lockout_active
    assert r.blockers == ()


def test_lockout_lifts_after_close_once_next_baseline_exists(holding: Setup) -> None:
    db, pf, mon = holding
    evaluate_at(db, pf, mon, et(MON, 14), {"AAPL": "94.00"})
    close(mon, "AAPL", MON, "94.00")
    r = evaluate_at(db, pf, mon, et(MON, 17), {"AAPL": "94.00"})  # after close: current = TUE
    assert r.current_session == TUE
    assert not r.lockout_active


# ------------------------------------------------------ repeat breaches


def test_two_breaches_within_five_sessions_halt(holding: Setup) -> None:
    db, pf, mon = holding
    evaluate_at(db, pf, mon, et(MON, 14), {"AAPL": "94.00"})  # breach 1 (MON)
    close(mon, "AAPL", MON, "100.00")
    evaluate_at(db, pf, mon, et(TUE, 11), {"AAPL": "100.00"})
    close(mon, "AAPL", TUE, "100.00")
    evaluate_at(db, pf, mon, et(WED, 11), {"AAPL": "100.00"})
    close(mon, "AAPL", WED, "100.00")
    r = evaluate_at(db, pf, mon, et(THU, 11), {"AAPL": "94.00"})  # breach 2 (4th session)
    assert r.recent_lockouts == (MON, THU)
    assert events(db, LossEventKind.DAILY_REPEAT_HALT) == [THU]
    assert halt_reasons(db)[-1] == "DAILY_LOSS_REPEAT"


@pytest.mark.parametrize(("second", "repeat"), [(FRI, True), (NEXT_MON, False)])
def test_five_session_window_boundary(holding: Setup, second: date, repeat: bool) -> None:
    """FRI is the 5th session counting MON; NEXT_MON is the 6th (MON falls out)."""
    db, pf, mon = holding
    evaluate_at(db, pf, mon, et(MON, 14), {"AAPL": "94.00"})
    day, prev = TUE, MON
    while day < second:
        close(mon, "AAPL", prev, "100.00")
        evaluate_at(db, pf, mon, et(day, 11), {"AAPL": "100.00"})
        prev, day = day, day + timedelta(days=1 if day.weekday() < 4 else 3)
    close(mon, "AAPL", prev, "100.00")
    r = evaluate_at(db, pf, mon, et(second, 11), {"AAPL": "94.00"})
    assert r.window is not None and len(r.window) == 5
    assert (MON in r.window) is repeat
    assert (events(db, LossEventKind.DAILY_REPEAT_HALT) == [second]) is repeat
    assert ("DAILY_LOSS_REPEAT" in halt_reasons(db)) is repeat


# ------------------------------------------------------------- weekly


def test_weekly_drawdown_halts_and_resume_does_not_bypass(holding: Setup) -> None:
    db, pf, mon = holding
    plan = [(MON, None, "100.00"), (TUE, MON, "95.50"), (WED, TUE, "91.00"),
            (THU, WED, "87.40")]  # fmt: skip
    prices = {MON: "100.00", TUE: "95.50", WED: "91.00"}
    r: LossLimitReport | None = None
    for day, prev, mark_price in plan:
        if prev is not None:
            close(mon, "AAPL", prev, prices[prev])
        r = evaluate_at(db, pf, mon, et(day, 11), {"AAPL": mark_price})
    assert r is not None and r.weekly is not None
    assert r.weekly.baseline == D(5000)  # fixed all week
    assert r.weekly.loss == D("252.00") and r.weekly.breached
    assert r.daily is not None and not r.daily.breached  # 72 vs Wednesday close
    assert events(db, LossEventKind.WEEKLY_HALT) == [MON]
    assert events(db, LossEventKind.DAILY_LOCKOUT) == []
    assert halt_reasons(db)[-1] == "WEEKLY_DRAWDOWN"
    with db.transaction() as tx:
        run = ReconciliationRunsRepository().append(tx, ReconciliationResult.OK, None, et(THU, 12))
        outcome = TradingStateRepository().resume(
            tx, detail="operator", actor="operator", at=et(THU, 12), reconciliation_run_id=run
        )
    assert outcome is ResumeOutcome.RESUMED
    r = evaluate_at(db, pf, mon, et(THU, 13), {"AAPL": "90.00"})  # partly recovered
    assert r.weekly_breach_active and LossBlocker.WEEKLY_LIMIT_ACTIVE in r.blockers
    assert events(db, LossEventKind.WEEKLY_HALT) == [MON]  # not duplicated
    with db.read() as s:
        assert not TradingStateRepository().get(s).halted  # resume stands; block remains
    close(mon, "AAPL", THU, "90.00")
    close(mon, "AAPL", FRI, "90.00")
    r = evaluate_at(db, pf, mon, et(NEXT_MON, 11), {"AAPL": "90.00"})
    assert not r.weekly_breach_active  # new week, new fixed baseline
    assert r.weekly_baseline is not None and r.weekly_baseline.equity == D("4800.00")


def test_weekly_baseline_is_fixed_within_the_week(holding: Setup) -> None:
    db, pf, mon = holding
    first = evaluate_at(db, pf, mon, et(MON, 11), {"AAPL": "100.00"}).weekly_baseline
    close(mon, "AAPL", MON, "120.00")
    later = evaluate_at(db, pf, mon, et(TUE, 11), {"AAPL": "120.00"})
    assert later.weekly_baseline == first
    assert later.daily_baseline is not None and later.daily_baseline.equity == D("5400.00")


@pytest.mark.parametrize(
    ("now_day", "week_start", "reference"),
    [
        (date(2026, 11, 30), date(2026, 11, 30), date(2026, 11, 27)),  # after Thanksgiving wk
        (date(2026, 11, 27), date(2026, 11, 23), date(2026, 11, 20)),  # inside the short week
        (date(2026, 12, 28), date(2026, 12, 28), date(2026, 12, 24)),  # Christmas Friday off
        (date(2027, 1, 4), date(2027, 1, 4), date(2026, 12, 31)),  # New Year Friday off
        (date(2027, 1, 19), date(2027, 1, 18), date(2027, 1, 15)),  # MLK Monday holiday
    ],
)
def test_holiday_shortened_weeks(setup: Setup, now_day: date, week_start: date,
                                 reference: date) -> None:  # fmt: skip
    db, pf, mon = setup
    r = evaluate_at(db, pf, mon, et(now_day, 10), {})
    assert r.week_start == week_start
    assert r.weekly_baseline is not None
    assert r.weekly_baseline.reference_session == reference
    assert r.weekly_baseline.applies_to == week_start


# ------------------------------------------------------------ overnight gaps


@pytest.mark.parametrize(
    ("open_price", "daily", "weekly"),
    [("95.00", True, False), ("89.00", True, False), ("87.00", True, True)],
)
def test_overnight_gap_counts_toward_limits(holding: Setup, open_price: str, daily: bool,
                                            weekly: bool) -> None:  # fmt: skip
    db, pf, mon = holding
    evaluate_at(db, pf, mon, et(MON, 15), {"AAPL": "100.00"})
    close(mon, "AAPL", MON, "100.00")
    r = evaluate_at(db, pf, mon, et(TUE, 9, 31), {"AAPL": open_price})  # first valid price
    (gap,) = r.gaps
    assert gap.prior_close == D("100.00") and gap.qty_at_close == D(20)
    assert gap.pnl == D(20) * (D(open_price) - D(100))
    assert gap.pct == (D(open_price) - D(100)) / D(100)
    assert (MON + timedelta(days=1) in events(db, LossEventKind.DAILY_LOCKOUT)) is daily
    assert (MON in events(db, LossEventKind.WEEKLY_HALT)) is weekly


# ------------------------------------------------ missing data blocks entries


def test_missing_or_stale_marks_block_and_skip_evaluation(holding: Setup) -> None:
    db, pf, mon = holding
    r = evaluate_at(db, pf, mon, et(MON, 11), {})  # held position, no mark
    assert r.equity is None and r.daily is None
    assert LossBlocker.EQUITY_UNKNOWN in r.blockers
    evaluate_at(db, pf, mon, et(MON, 11), {"AAPL": "80.00"})  # mark exists ...
    r = mon.evaluate(now=et(MON, 11, 30))  # ... but 31 minutes old now
    assert LossBlocker.EQUITY_UNKNOWN in r.blockers
    assert events(db, LossEventKind.DAILY_LOCKOUT) == [MON]  # only the fresh evaluation


def test_incomplete_calendar_blocks(tmp_path: Path) -> None:
    db, pf = new_ledger(tmp_path / "nocal.sqlite3", with_calendar=False)
    mon = monitor(db, pf)
    r = mon.evaluate(now=et(MON, 10))
    assert LossBlocker.CALENDAR_UNAVAILABLE in r.blockers
    assert r.daily_baseline is None and r.current_session is None
    with db.read() as s:
        assert s.scalar("SELECT count(*) FROM equity_snapshots") == 0
    db.close()


def test_stale_calendar_blocks(setup: Setup) -> None:
    db, pf, mon = setup
    evaluate_at(db, pf, mon, et(MON, 10), {})
    r = mon.evaluate(now=et(THU, 10))  # calendar fetched Monday: stale for Thursday
    assert LossBlocker.CALENDAR_UNAVAILABLE in r.blockers


# ------------------------------------------------- persistence & idempotency


def test_restart_preserves_baselines_events_and_halts(holding: Setup, tmp_path: Path) -> None:
    db, pf, mon = holding
    close(mon, "AAPL", MON, "100.00")
    evaluate_at(db, pf, mon, et(TUE, 9, 31), {"AAPL": "87.00"})
    path = db.path
    db.close()
    again = Database.open(path, RuntimeMode.SIMULATE)
    pf2 = VirtualPortfolio(again, PF_CONFIG)
    mon2 = LossLimitMonitor(again, pf2, CONFIG)
    with again.read() as s:
        r = mon2.report(s, now=et(TUE, 9, 32))
        state = TradingStateRepository().get(s)
    assert r.lockout_active and r.weekly_breach_active
    assert state.halted and state.reason_code == "WEEKLY_DRAWDOWN"
    assert r.daily_baseline is not None and r.daily_baseline.equity == D(5000)
    again.close()


def test_repeated_evaluation_never_duplicates(holding: Setup) -> None:
    db, pf, mon = holding
    for minute in range(5):
        evaluate_at(db, pf, mon, et(MON, 14, minute), {"AAPL": "87.00"})
    assert events(db, LossEventKind.DAILY_LOCKOUT) == [MON]
    assert events(db, LossEventKind.WEEKLY_HALT) == [MON]
    assert halt_reasons(db).count("WEEKLY_DRAWDOWN") == 1
    with db.read() as s:
        assert s.scalar("SELECT count(*) FROM equity_snapshots") == 2


def test_report_is_read_only(holding: Setup) -> None:
    db, _, mon = holding
    with db.read() as s:
        r = mon.report(s, now=et(MON, 11))  # no baselines yet: report must not create them
        assert s.scalar("SELECT count(*) FROM equity_snapshots") == 0
    assert LossBlocker.SESSION_BASELINE_MISSING in r.blockers


def test_session_closes_are_immutable(setup: Setup) -> None:
    _, _, mon = setup
    c = SessionClose("AAPL", MON, D("100.00"), CloseSource.SESSION_CLOSE_BAR,
                     as_of=et(MON, 16), received_at=et(MON, 16, 5))  # fmt: skip
    assert mon.record_session_close(c) is IngestResult.INSERTED
    assert mon.record_session_close(c) is IngestResult.DUPLICATE
    changed = SessionClose("AAPL", MON, D("100.01"), CloseSource.SESSION_CLOSE_BAR,
                           as_of=et(MON, 16), received_at=et(MON, 16, 5))  # fmt: skip
    assert mon.record_session_close(changed) is IngestResult.CONFLICT


# ------------------------------------------------------------------ gap stress


def test_gap_stress_per_position_and_portfolio(holding: Setup) -> None:
    db, pf, mon = holding
    r = evaluate_at(db, pf, mon, et(MON, 11), {"AAPL": "100.00"})
    stress = r.gap_stress
    assert stress is not None
    assert stress.denominator == D(5000)  # recorded baseline equity, not current equity
    (aapl,) = stress.positions
    assert (aapl.market_value, aapl.stressed_loss, aapl.limit) == (
        D("2000.00"), D("300.0000"), D("150.00"))  # fmt: skip
    assert stress.positions_exceeded == ("AAPL",)
    assert not stress.portfolio_exceeded
    order = make_order(db, qty="10", limit="150.00", symbol="MSFT")
    reserve(pf, db, order)  # 1500 working commitment, stressed at 15% = 225
    r = evaluate_at(db, pf, mon, et(MON, 11, 1), {"AAPL": "100.00"})
    assert r.gap_stress is not None
    assert r.gap_stress.portfolio_stressed == D("525.000000")
    assert r.gap_stress.portfolio_exceeded
    assert LossBlocker.GAP_STRESS_PORTFOLIO_EXCEEDED in r.blockers


def test_config_matches_approved_parameters() -> None:
    assert (CONFIG.daily_pct, CONFIG.weekly_pct) == (D("0.02"), D("0.05"))
    assert (CONFIG.escalation_count, CONFIG.escalation_sessions) == (2, 5)
    assert (CONFIG.gap_scenario_pct, CONFIG.gap_position_pct, CONFIG.gap_portfolio_pct) == (
        D("0.15"), D("0.03"), D("0.10"))  # fmt: skip
    assert SnapshotKind.WEEK_BASELINE.value == "WEEK_BASELINE"
