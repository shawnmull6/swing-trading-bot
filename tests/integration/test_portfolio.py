"""Step 6: virtual portfolio accounting against a simulation ledger."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import HaltReason, MarkSource, OrderStatus, RuntimeMode, Side
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.domain.valuation import MarkStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.portfolio import (
    AccountingError,
    CashActivityKind,
    EntryBlocker,
    FillOutcome,
    InsufficientBuyingCapacityError,
    PortfolioSnapshot,
    VirtualPortfolio,
)
from swing_trading_bot.ledger.repositories import (
    ActivitiesRepository,
    CashEntriesRepository,
    CashEntryKind,
    CashEntryRecord,
    FillsRepository,
    OrdersRepository,
    PositionsRepository,
    ReservationsRepository,
    ReservationStatus,
    TradingStateRepository,
)
from tests.support.portfolio import (
    CONFIG,
    NOW,
    cash_activity,
    fill,
    finish,
    make_order,
    mark,
    new_ledger,
    reserve,
    seed_calendar,
)

D = Decimal
Ledger = tuple[Database, VirtualPortfolio]


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    db, pf = new_ledger(tmp_path / "pf.sqlite3")
    yield db, pf
    db.close()


def snap(db: Database, pf: VirtualPortfolio, now: datetime = NOW) -> PortfolioSnapshot:
    with db.read() as s:
        return pf.snapshot(s, now=now)


def halted(db: Database) -> tuple[bool, str | None]:
    with db.read() as s:
        st = TradingStateRepository().get(s)
    return st.halted, st.reason_code


def counts(db: Database) -> tuple[int, int, int]:
    with db.read() as s:
        return (
            int(str(s.scalar("SELECT count(*) FROM broker_activities"))),
            int(str(s.scalar("SELECT count(*) FROM fills"))),
            int(str(s.scalar("SELECT count(*) FROM cash_entries"))),
        )


def check_ok(pf: VirtualPortfolio, now: datetime = NOW) -> None:
    report = pf.check_invariants(now=now)
    assert report.ok, report.violations


# ----------------------------------------------------------------- initial state


def test_initial_portfolio(ledger: Ledger) -> None:
    db, pf = ledger
    s = snap(db, pf)
    b = s.balances
    assert (b.cash, b.settled_cash, b.available_cash) == (D(5000), D(5000), D(5000))
    assert (b.buy_commitments, b.deployed_capital, b.capital_headroom) == (D(0), D(0), D(5000))
    assert b.buying_capacity == D(5000)
    assert b.max_capital_allocation == D(5000)
    assert s.market_value == D(0)
    assert s.equity == D(5000)
    assert s.entry_eligible
    check_ok(pf)
    with db.transaction() as tx, pytest.raises(AccountingError, match="already exists"):
        VirtualPortfolio.initialize(
            tx, name="main", mode=RuntimeMode.SIMULATE, initial_capital=D(5000),
            max_capital_allocation=D(5000), account_profile="x", at=NOW,
        )  # fmt: skip


# -------------------------------------------------------------- reservations


def test_reservation_reduces_available_not_cash(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    assert reserve(pf, db, order) == D("1502.5000")
    b = snap(db, pf).balances
    assert b.cash == D(5000)
    assert b.available_cash == D("3497.5000")
    assert b.deployed_capital == D("1502.5000")
    assert b.buying_capacity == D("3497.5000")
    check_ok(pf)


def test_reservation_rounds_up_and_includes_fee_estimate(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="3", limit="33.33")
    assert reserve(pf, db, order, fee="0.00001") == D("99.9901")  # 99.99001 rounded up


def test_reservation_refused_beyond_capacity(ledger: Ledger) -> None:
    db, pf = ledger
    big = make_order(db, qty="34", limit="150.00")  # 5100 > 5000
    with pytest.raises(InsufficientBuyingCapacityError):
        reserve(pf, db, big)
    with db.read() as s:
        assert ReservationsRepository().get(s, big.client_order_id) is None
    sell = make_order(db, side=Side.SELL, limit="10.00")
    with pytest.raises(AccountingError, match="limit buy"):
        reserve(pf, db, sell)
    accepted = make_order(db, status=OrderStatus.ACCEPTED)
    with pytest.raises(AccountingError, match="before submission"):
        reserve(pf, db, accepted)
    plain = make_order(db)
    with db.transaction() as tx, pytest.raises(AccountingError, match="negative"):
        pf.reserve_buy(tx, plain.client_order_id, fee_estimate=D("-1"), now=NOW)


def test_full_fill_consumes_reservation(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    result = pf.process_fill(fill(order, "10", "150.00"), now=NOW)  # price improvement
    assert result.outcome is FillOutcome.APPLIED
    assert not result.halted
    assert result.settles_on == date(2026, 11, 10)
    b = snap(db, pf).balances
    assert b.cash == D("3500.00")
    assert b.cost_basis_exposure == D("1500.00")
    # The reservation shrinks by qty x limit; the $2.50 price improvement is already in cash.
    assert b.buy_commitments == D("0")
    with db.transaction() as tx, pytest.raises(AccountingError, match="terminal"):
        pf.close_reservation(tx, order.client_order_id, now=NOW)  # order still working
    assert finish(pf, db, order, OrderStatus.FILLED) == D("0")
    with db.read() as s:
        res = ReservationsRepository().get(s, order.client_order_id)
        o = OrdersRepository().require(s, order.client_order_id)
    assert res is not None and res.status is ReservationStatus.CONSUMED
    assert (o.filled_qty, o.avg_fill_price, o.status) == (D(10), D("150.0000"), OrderStatus.FILLED)
    b = snap(db, pf).balances
    assert (b.available_cash, b.deployed_capital) == (D("3500.00"), D("1500.00"))


def test_partial_fill_then_cancel_releases_remainder(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    pf.process_fill(fill(order, "4", "150.25"), now=NOW)
    b = snap(db, pf).balances
    assert b.cash == D("4399.00")  # 5000 - 4 x 150.25
    assert b.buy_commitments == D("901.5000")
    assert b.available_cash == D("3497.5000")
    with db.transaction() as tx, pytest.raises(AccountingError, match="terminal"):
        pf.close_reservation(tx, order.client_order_id, now=NOW)
    released = finish(pf, db, order, OrderStatus.CANCELED)
    with db.transaction() as tx:
        assert pf.close_reservation(tx, order.client_order_id, now=NOW) == 0  # idempotent
    assert released == D("901.5000")
    s = snap(db, pf)
    assert s.balances.available_cash == D("4399.00")
    assert s.balances.deployed_capital == D("601.00")
    (pos,) = s.positions
    assert (pos.qty, pos.cost_basis, pos.avg_cost) == (D(4), D("601.00"), D("150.2500"))
    check_ok(pf)


# --------------------------------------------------------------------- sells


def _buy(db: Database, pf: VirtualPortfolio, qty: str, price: str, **kw: object) -> None:
    order = make_order(db, qty=qty, limit=price, **kw)  # type: ignore[arg-type]
    reserve(pf, db, order)
    assert pf.process_fill(fill(order, qty, price), now=NOW).outcome is FillOutcome.APPLIED
    finish(pf, db, order, OrderStatus.FILLED)


def _sell(db: Database, pf: VirtualPortfolio, qty: str, price: str, *, at: datetime = NOW,
          fees: str = "0", symbol: str = "AAPL") -> object:  # fmt: skip
    order = make_order(db, side=Side.SELL, qty=qty, limit=price, symbol=symbol)
    return pf.process_fill(fill(order, qty, price, at=at, fees=fees), now=at)


def test_sells_with_gain_and_loss_keep_average_cost(ledger: Ledger) -> None:
    db, pf = ledger
    _buy(db, pf, "10", "100.00")
    _sell(db, pf, "4", "110.00")
    with db.read() as s:
        pos = PositionsRepository().get(s, 1, "AAPL")
        last = FillsRepository().list_in_application_order(s)[-1]
    assert pos is not None
    assert (pos.qty, pos.cost_basis, pos.realized_pnl) == (D(6), D("600.00"), D("40.00"))
    assert last.realized_pnl == D("40.00")
    assert last.avg_cost_before == D("100.0000")
    _sell(db, pf, "6", "90.00", fees="0.05")
    with db.read() as s:
        pos = PositionsRepository().get(s, 1, "AAPL")
    assert pos is not None
    assert (pos.qty, pos.cost_basis, pos.realized_pnl) == (D(0), D(0), D("-20.05"))
    assert pos.opened_at is None
    summary = snap(db, pf)
    assert summary.realized_pnl == D("-20.05")
    assert summary.balances.cash == D("4979.95")
    check_ok(pf)


def test_repeating_average_cost_leaves_no_residue(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="3", limit="100.01")
    reserve(pf, db, order, fee="0.01")
    pf.process_fill(fill(order, "3", "100.01", fees="0.01"), now=NOW)  # cost 300.04
    _sell(db, pf, "1", "100.00")
    with db.read() as s:
        pos = PositionsRepository().get(s, 1, "AAPL")
    assert pos is not None
    assert pos.cost_basis == D("200.0267")  # 300.04 - round(300.04/3, 4)
    _sell(db, pf, "2", "100.00")
    with db.read() as s:
        pos = PositionsRepository().get(s, 1, "AAPL")
    assert pos is not None
    assert (pos.qty, pos.cost_basis) == (D(0), D(0))
    assert pos.realized_pnl == D("-0.04")  # exactly the fee + rounding-free price difference
    check_ok(pf)


# ---------------------------------------------------------------- settlement


def test_sell_proceeds_unsettled_until_t_plus_1_over_bank_holiday(tmp_path: Path) -> None:
    tue = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    db, pf = new_ledger(tmp_path / "s.sqlite3", calendar_at=tue)
    _buy(db, pf, "10", "100.00")
    result = _sell(db, pf, "10", "120.00", at=tue)
    assert result.settles_on == date(2026, 11, 12)  # type: ignore[attr-defined]
    for when, settled in [
        (tue, D("4000.00")),
        (datetime(2026, 11, 11, 20, 0, tzinfo=UTC), D("4000.00")),  # Veterans Day: not yet
        (datetime(2026, 11, 12, 4, 59, tzinfo=UTC), D("4000.00")),  # 23:59 ET Nov 11
        (datetime(2026, 11, 12, 5, 0, tzinfo=UTC), D("5200.00")),  # 00:00 ET Nov 12
    ]:
        b = snap(db, pf, when).balances
        assert b.cash == D("5200.00")
        assert b.settled_cash == settled, when
    b = snap(db, pf, tue).balances
    assert b.unsettled_proceeds == D("1200.00")
    assert b.buying_capacity == D("4000.00")
    db.close()


@pytest.mark.parametrize(
    ("trade_at", "settles", "case"),
    [
        (datetime(2026, 4, 2, 15, 0, tzinfo=UTC), date(2026, 4, 6), "Good Friday (VERIFIED)"),
        (datetime(2026, 7, 2, 15, 0, tzinfo=UTC), date(2026, 7, 6), "Jul 3 (VERIFIED)"),
        (datetime(2026, 11, 25, 16, 0, tzinfo=UTC), date(2026, 11, 27), "Thanksgiving (ASSUMED)"),
        (datetime(2026, 12, 31, 16, 0, tzinfo=UTC), date(2027, 1, 4), "year end (ASSUMED)"),
        (datetime(2026, 11, 6, 21, 30, tzinfo=UTC), date(2026, 11, 9), "Friday 16:30 ET -> Mon"),
    ],
)
def test_settlement_dates_through_holidays(
    tmp_path: Path, trade_at: datetime, settles: date, case: str
) -> None:
    db, pf = new_ledger(tmp_path / "h.sqlite3", calendar_at=trade_at)
    order = make_order(db, qty="1", limit="10.00")
    reserve(pf, db, order)
    result = pf.process_fill(fill(order, "1", "10.00", at=trade_at), now=trade_at)
    assert result.settles_on == settles, case
    db.close()


def test_missing_calendar_leaves_settlement_unresolved(tmp_path: Path) -> None:
    db, pf = new_ledger(tmp_path / "nc.sqlite3", with_calendar=False)
    _buy(db, pf, "10", "100.00")
    result = _sell(db, pf, "10", "105.00")
    assert result.settles_on is None  # type: ignore[attr-defined]
    s = snap(db, pf)
    assert s.balances.unsettled_proceeds == D("1050.00")
    assert s.balances.settled_cash == D("4000.00")
    assert EntryBlocker.UNRESOLVED_SETTLEMENT in s.entry_blockers
    assert not s.entry_eligible
    assert pf.resolve_settlements(now=NOW) == 0  # still no calendar: never guessed
    seed_calendar(db, NOW)
    assert pf.resolve_settlements(now=NOW) == 2  # the buy and the sell
    s = snap(db, pf)
    assert EntryBlocker.UNRESOLVED_SETTLEMENT not in s.entry_blockers
    with db.read() as r:
        entries = CashEntriesRepository().list_for_portfolio(r, 1)
    assert {e.settles_on for e in entries if e.kind.value in {"BUY", "SELL"}} == {
        date(2026, 11, 10)
    }
    check_ok(pf)
    db.close()


# ------------------------------------------------------- capital ceiling


def test_aggregate_capital_cap_across_positions(ledger: Ledger) -> None:
    db, pf = ledger
    orders = [
        make_order(db, qty="10", limit="100.00", symbol=sym)
        for sym in ("AAPL", "MSFT", "SPY", "QQQ")
    ]
    for o in orders:
        reserve(pf, db, o)
    b = snap(db, pf).balances
    assert (b.deployed_capital, b.buying_capacity) == (D(4000), D(1000))
    fifth = make_order(db, qty="1", limit="1000.01", symbol="IWM")
    with pytest.raises(InsufficientBuyingCapacityError):
        reserve(pf, db, fifth)
    pf.process_fill(fill(orders[0], "10", "100.00"), now=NOW)
    b = snap(db, pf).balances
    assert b.deployed_capital == D(4000)  # commitment converted to cost basis, not doubled
    check_ok(pf)


def test_profits_compound_without_raising_the_ceiling(tmp_path: Path) -> None:
    tue = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    db, pf = new_ledger(tmp_path / "p.sqlite3", calendar_at=tue)
    _buy(db, pf, "10", "100.00")
    _sell(db, pf, "10", "200.00", at=tue)  # +1000 realized, settles Thu Nov 12
    later = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    s = snap(db, pf, later)
    b = s.balances
    assert b.cash == D("6000.00")
    assert b.settled_cash == D("6000.00")
    assert b.max_capital_allocation == D("5000")
    assert b.buying_capacity == D("5000")  # ceiling binds, not the 6000 of cash
    assert s.equity == D("6000.00")
    with db.read() as r:
        assert r.scalar("SELECT max_capital_allocation FROM portfolios") == "5000"
    db.close()


def test_losses_make_cash_the_binding_constraint(ledger: Ledger) -> None:
    db, pf = ledger
    _buy(db, pf, "10", "100.00")
    _sell(db, pf, "10", "50.00")  # -500, unsettled until tomorrow
    b = snap(db, pf, datetime(2026, 11, 10, 15, 0, tzinfo=UTC)).balances
    assert b.cash == D("4500.00")
    assert b.capital_headroom == D(5000)
    assert b.buying_capacity == D("4500.00")


# --------------------------------------------------------- dividends & fees


def test_dividends_and_fees_are_auditable_cash_entries(ledger: Ledger) -> None:
    db, pf = ledger
    _buy(db, pf, "10", "100.00")
    div = cash_activity(CashActivityKind.DIVIDEND, "2.40", activity_id="div-1")
    fee = cash_activity(CashActivityKind.FEE, "-0.02", activity_id="fee-1")
    assert pf.process_cash_activity(div, now=NOW).outcome is FillOutcome.APPLIED
    assert pf.process_cash_activity(fee, now=NOW).outcome is FillOutcome.APPLIED
    assert pf.process_cash_activity(div, now=NOW).outcome is FillOutcome.DUPLICATE
    b = snap(db, pf).balances
    assert b.cash == D("4002.38")
    assert (b.dividends, b.fee_entries) == (D("2.40"), D("-0.02"))
    assert b.settled_cash == b.cash
    with db.read() as s:
        entries = CashEntriesRepository().list_for_portfolio(s, 1)
        act = ActivitiesRepository().get(s, "div-1")
    kinds = [(e.kind.value, e.activity_id) for e in entries]
    assert ("DIVIDEND", "div-1") in kinds and ("FEE", "fee-1") in kinds
    assert act is not None and act.net_amount == D("2.40")
    check_ok(pf)
    changed = cash_activity(CashActivityKind.DIVIDEND, "2.41", activity_id="div-1")
    result = pf.process_cash_activity(changed, now=NOW)
    assert result.outcome is FillOutcome.CONFLICT and result.halted
    assert snap(db, pf).balances.cash == D("4002.38")


# ------------------------------------------------------ duplicates/conflicts


def test_duplicate_fill_is_exact_noop(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    f = fill(order, "4", "150.25", activity_id="act-dup")
    first = pf.process_fill(f, now=NOW)
    before = (counts(db), snap(db, pf).balances)
    for _ in range(3):
        again = pf.process_fill(f, now=NOW + timedelta(minutes=1))
        assert again.outcome is FillOutcome.DUPLICATE
        assert again.fill_id == first.fill_id
        assert not again.halted
    assert (counts(db), snap(db, pf).balances) == before
    assert halted(db) == (True, "INITIALIZED")  # untouched by duplicates


def test_conflicting_fill_halts_without_touching_books(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    pf.process_fill(fill(order, "4", "150.25", activity_id="act-x"), now=NOW)
    before = (counts(db), snap(db, pf).balances)
    result = pf.process_fill(fill(order, "4", "150.20", activity_id="act-x"), now=NOW)
    assert result.outcome is FillOutcome.CONFLICT
    assert HaltReason.RECON_EXECUTION in result.halt_reasons
    assert (counts(db), snap(db, pf).balances) == before
    assert halted(db) == (True, "RECON_EXECUTION")


def test_non_bot_fill_recorded_as_unexplained_and_halts(ledger: Ledger) -> None:
    db, pf = ledger
    result = pf.process_fill(
        fill(None, "5", "10.00", client_order_id="manual-123", activity_id="act-ext"), now=NOW
    )
    assert result.outcome is FillOutcome.UNATTRIBUTED and result.halted
    with db.read() as s:
        act = ActivitiesRepository().get(s, "act-ext")
    assert act is not None and act.attribution.value == "UNEXPLAINED"
    assert counts(db)[1] == 0  # no fill booked to the virtual ledger
    assert snap(db, pf).balances.cash == D(5000)


def test_unknown_bot_order_fill_booked_as_orphan_then_halts(ledger: Ledger) -> None:
    db, pf = ledger
    coid = new_client_order_id()
    result = pf.process_fill(fill(None, "2", "50.00", client_order_id=coid), now=NOW)
    assert result.outcome is FillOutcome.ORPHAN
    assert result.halted
    with db.read() as s:
        o = OrdersRepository().require(s, coid)
        pos = PositionsRepository().get(s, 1, "AAPL")
    assert o.status is OrderStatus.ORPHAN and o.filled_qty == D(2)
    assert pos is not None and pos.qty == D(2)
    assert halted(db) == (True, "RECON_EXECUTION")


# ----------------------------------------------- invariant breach after fill


def test_overfill_is_recorded_then_halts(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="100.00")
    reserve(pf, db, order)
    result = pf.process_fill(fill(order, "12", "100.00"), now=NOW)
    assert result.outcome is FillOutcome.APPLIED
    assert result.halted
    assert HaltReason.LEDGER_INVARIANT in result.halt_reasons
    assert {v.invariant for v in result.violations} >= {"order_fill_consistency"}
    with db.read() as s:
        assert OrdersRepository().require(s, order.client_order_id).filled_qty == D(12)
    assert snap(db, pf).balances.cash == D("3800.00")
    assert halted(db) == (True, "LEDGER_INVARIANT")


def test_oversell_records_negative_position_then_halts(ledger: Ledger) -> None:
    db, pf = ledger
    _buy(db, pf, "3", "100.00")
    result = _sell(db, pf, "5", "100.00")
    assert result.halted  # type: ignore[attr-defined]
    s = snap(db, pf)
    (pos,) = s.positions
    assert pos.qty == D(-2)
    assert EntryBlocker.NEGATIVE_POSITION in s.entry_blockers
    names = {v.invariant for v in pf.check_invariants(now=NOW).violations}
    assert "non_negative_positions" in names
    with db.read() as r:
        events = [
            e for e in TradingStateRepository().events(r) if e.reason_code == "LEDGER_INVARIANT"
        ]
    assert len(events) == 1  # repeated checks do not spam identical halts


def test_fill_exceeding_cash_is_recorded_then_halts(ledger: Ledger) -> None:
    db, pf = ledger
    coid = new_client_order_id()
    result = pf.process_fill(fill(None, "100", "100.00", client_order_id=coid), now=NOW)
    assert result.halted
    assert snap(db, pf).balances.cash == D("-5000.00")
    names = {v.invariant for v in result.violations}
    assert "capital_limits" in names


# ---------------------------------------------------------- crash / restart


@pytest.mark.parametrize(
    "stage", ["after_activity", "after_fill", "after_cash", "after_position", "before_commit"]
)
def test_crash_mid_update_rolls_back_and_replays_once(ledger: Ledger, stage: str) -> None:
    db, pf = ledger
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    f = fill(order, "10", "150.25", activity_id="act-crash")
    before = (counts(db), snap(db, pf).balances)

    class CrashError(Exception):
        pass

    def hook(point: str) -> None:
        if point == stage:
            raise CrashError

    with pytest.raises(CrashError):
        pf.process_fill(f, now=NOW, hook=hook)
    assert (counts(db), snap(db, pf).balances) == before
    assert pf.process_fill(f, now=NOW).outcome is FillOutcome.APPLIED
    assert pf.process_fill(f, now=NOW).outcome is FillOutcome.DUPLICATE
    assert snap(db, pf).balances.cash == D("3497.50")
    check_ok(pf)


def test_process_kill_mid_update_then_restart(tmp_path: Path) -> None:
    path = tmp_path / "kill.sqlite3"
    db, pf = new_ledger(path)
    order = make_order(db, qty="10", limit="150.25")
    reserve(pf, db, order)
    db.close()
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from swing_trading_bot.domain.enums import RuntimeMode
        from swing_trading_bot.ledger.db import Database
        from swing_trading_bot.ledger.portfolio import VirtualPortfolio
        from tests.support.portfolio import CONFIG, NOW, fill
        from swing_trading_bot.ledger.repositories import OrdersRepository
        db = Database.open(Path({str(path)!r}), RuntimeMode.SIMULATE)
        with db.read() as s:
            order = OrdersRepository().require(s, {order.client_order_id!r})
        pf = VirtualPortfolio(db, CONFIG)
        def hook(point):
            if point == "after_position":
                os._exit(29)
        pf.process_fill(fill(order, "10", "150.25", activity_id="act-kill"), now=NOW, hook=hook)
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", script],
        capture_output=True, check=False, timeout=60, cwd=Path(__file__).resolve().parents[2],
    )  # fmt: skip
    assert result.returncode == 29, result.stderr.decode()
    with Database.open(path, RuntimeMode.SIMULATE) as again:
        pf2 = VirtualPortfolio(again, CONFIG)
        assert counts(again) == (0, 0, 1)  # only the opening deposit
        with again.read() as s:
            o = OrdersRepository().require(s, order.client_order_id)
        r = pf2.process_fill(fill(o, "10", "150.25", activity_id="act-kill"), now=NOW)
        assert r.outcome is FillOutcome.APPLIED
        assert snap(again, pf2).balances.cash == D("3497.50")
        check_ok(pf2)


# --------------------------------------------------------------- price marks


def _with_position(db: Database, pf: VirtualPortfolio) -> None:
    _buy(db, pf, "10", "100.00")


def test_missing_mark_blocks_entries_and_valuation(ledger: Ledger) -> None:
    db, pf = ledger
    _with_position(db, pf)
    s = snap(db, pf)
    assert s.positions[0].mark_assessment.status is MarkStatus.MISSING
    assert s.market_value is None and s.equity is None and s.unrealized_pnl is None
    assert s.market_exposure is None
    assert EntryBlocker.STALE_OR_MISSING_MARKS in s.entry_blockers
    assert s.balances.cost_basis_exposure == D("1000.00")  # cost exposure still known


def test_fresh_mark_values_position(ledger: Ledger) -> None:
    db, pf = ledger
    _with_position(db, pf)
    with db.transaction() as tx:
        pf.record_mark(tx, mark("AAPL", "110.00", as_of=NOW - timedelta(minutes=2), received=NOW))
    s = snap(db, pf)
    (pos,) = s.positions
    assert pos.mark_assessment.status is MarkStatus.FRESH
    assert (pos.market_value, pos.unrealized_pnl) == (D("1100.00"), D("100.00"))
    assert s.market_value == D("1100.00")
    assert s.equity == D("5100.00")
    assert s.market_exposure == D("1100.00")
    assert s.entry_eligible


@pytest.mark.parametrize(
    ("as_of", "received", "now", "status"),
    [
        (NOW - timedelta(minutes=20), NOW, NOW, MarkStatus.STALE),  # older than 15 min in session
        # not refreshed within max sync age:
        (NOW - timedelta(minutes=6), NOW - timedelta(minutes=5), NOW, MarkStatus.STALE),
        (NOW + timedelta(minutes=1), NOW, NOW, MarkStatus.FUTURE),
        (datetime(2026, 11, 6, 20, 59, tzinfo=UTC), NOW, NOW, MarkStatus.STALE),  # prior session
    ],
)  # fmt: skip
def test_unusable_marks(ledger: Ledger, as_of: datetime, received: datetime,
                        now: datetime, status: MarkStatus) -> None:  # fmt: skip
    db, pf = ledger
    _with_position(db, pf)
    with db.transaction() as tx:
        pf.record_mark(tx, mark("AAPL", "110.00", as_of=as_of, received=received))
    s = snap(db, pf, now)
    assert s.positions[0].mark_assessment.status is status
    assert s.equity is None
    assert not s.entry_eligible


def test_weekend_uses_last_completed_session(ledger: Ledger) -> None:
    db, pf = ledger
    _with_position(db, pf)
    saturday = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    seed_calendar(db, saturday)
    friday_close = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)
    thursday_close = datetime(2026, 11, 12, 20, 59, tzinfo=UTC)
    bar = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)  # daily bar stamped 00:00 ET
    for as_of, source, expected in [
        (friday_close, MarkSource.LATEST_TRADE, MarkStatus.FRESH),
        (thursday_close, MarkSource.LATEST_TRADE, MarkStatus.STALE),
        (bar, MarkSource.SESSION_CLOSE_BAR, MarkStatus.FRESH),
        (bar, MarkSource.LATEST_TRADE, MarkStatus.STALE),
    ]:
        with db.transaction() as tx:
            pf.record_mark(
                tx, mark("AAPL", "101.00", as_of=as_of, received=saturday, source=source)
            )
        assert snap(db, pf, saturday).positions[0].mark_assessment.status is expected, (
            as_of,
            source,
        )


def test_stale_calendar_makes_marks_unverifiable(ledger: Ledger) -> None:
    db, pf = ledger
    _with_position(db, pf)
    later = NOW + timedelta(days=3)
    with db.transaction() as tx:
        pf.record_mark(
            tx, mark("AAPL", "110.00", as_of=later - timedelta(minutes=1), received=later)
        )
    s = snap(db, pf, later)
    assert s.positions[0].mark_assessment.status is MarkStatus.UNVERIFIABLE
    assert s.equity is None


def test_values_are_stored_as_exact_text(ledger: Ledger) -> None:
    db, pf = ledger
    order = make_order(db, qty="7", limit="33.3333")
    reserve(pf, db, order)
    pf.process_fill(fill(order, "7", "0.5001", fees="0.0001"), now=NOW)
    with db.read() as s:
        rows = s.fetch_all("SELECT amount, typeof(amount) FROM cash_entries WHERE kind = 'BUY'")
        pos = s.fetch_one("SELECT cost_basis, typeof(cost_basis) FROM positions")
    assert rows == [("-3.5008", "text")]
    assert pos == ("3.5008", "text")


def test_snapshot_reports_fill_fees(ledger: Ledger) -> None:
    db, pf = ledger
    _with_position(db, pf)
    _sell(db, pf, "10", "100.00", fees="0.03")
    assert snap(db, pf).fill_fees == D("0.03")


# ------------------------------------------------- invariant detection coverage


def _raw(db: Database, sql: str, *params: object) -> None:
    conn = sqlite3.connect(db.path, isolation_level=None)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        ("extra_deposit", {"initial_deposit"}),  # balances in conservation
        ("position_cost", {"position_cost_replay", "cash_conservation"}),
        ("fill_cash", {"fill_cash_linkage", "cash_conservation"}),
        ("fill_realized", {"position_cost_replay"}),
        ("reservation", {"reservation_bounds"}),
    ],
)
def test_invariants_detect_ledger_tampering(
    ledger: Ledger, tamper: str, expected: set[str]
) -> None:
    db, pf = ledger
    _buy(db, pf, "10", "100.00")
    _sell(db, pf, "4", "110.00")
    pending = make_order(db, qty="1", limit="10.00", symbol="MSFT")
    reserve(pf, db, pending)
    check_ok(pf)
    if tamper == "extra_deposit":
        with db.transaction() as tx:
            CashEntriesRepository().append(
                tx,
                CashEntryRecord(portfolio_id=1, ts=NOW, kind=CashEntryKind.DEPOSIT,
                                amount=D("1"), trade_date=date(2026, 11, 9),
                                settles_on=date(2026, 11, 9)),
            )  # fmt: skip
    elif tamper == "position_cost":
        _raw(db, "UPDATE positions SET cost_basis = '599.99' WHERE symbol = 'AAPL'")
    elif tamper == "fill_cash":
        _raw(db, "DROP TRIGGER cash_entries_immutable")
        _raw(db, "UPDATE cash_entries SET amount = '-999.99' WHERE kind = 'BUY'")
    elif tamper == "fill_realized":
        _raw(db, "DROP TRIGGER fills_no_update")
        _raw(db, "UPDATE fills SET realized_pnl = '41' WHERE side = 'SELL'")
    else:
        with db.transaction() as tx:
            ReservationsRepository().set_remaining(tx, pending.client_order_id, D("11"), NOW)
    report = pf.check_invariants(now=NOW)
    assert expected <= {v.invariant for v in report.violations}
    assert halted(db) == (True, "LEDGER_INVARIANT")


def test_mark_after_todays_close_and_without_calendar(ledger: Ledger, tmp_path: Path) -> None:
    db, pf = ledger
    _with_position(db, pf)
    evening = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)  # 17:00 ET, after today's close
    for as_of, expected in [
        (datetime(2026, 11, 9, 20, 59, tzinfo=UTC), MarkStatus.FRESH),
        (datetime(2026, 11, 6, 20, 59, tzinfo=UTC), MarkStatus.STALE),
    ]:
        with db.transaction() as tx:
            pf.record_mark(tx, mark("AAPL", "101.00", as_of=as_of, received=evening))
        assert snap(db, pf, evening).positions[0].mark_assessment.status is expected
    bare, bare_pf = new_ledger(tmp_path / "bare.sqlite3", with_calendar=False)
    _buy(bare, bare_pf, "1", "10.00")
    with bare.transaction() as tx:
        bare_pf.record_mark(tx, mark("AAPL", "10.00", as_of=NOW, received=NOW))
    s = snap(bare, bare_pf)
    assert s.positions[0].mark_assessment.status is MarkStatus.UNVERIFIABLE
    assert s.equity is None
    bare.close()
