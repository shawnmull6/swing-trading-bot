"""Step 7 preliminary accounting review: fee signs, settlement durability/audit and
booking order vs execution order."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import HaltReason, OrderStatus, RuntimeMode, Side
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import ConstraintViolationError
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import (
    CashActivity,
    CashActivityKind,
    ConfirmedFill,
    FillOutcome,
    VirtualPortfolio,
)
from tests.support.loss_limits import CONFIG as LL_CONFIG
from tests.support.loss_limits import close, et, evaluate_at, hold
from tests.support.portfolio import (
    CONFIG,
    NOW,
    cash_activity,
    fill,
    finish,
    make_order,
    new_ledger,
    reserve,
    seed_calendar,
)

D = Decimal
Ledger = tuple[Database, VirtualPortfolio]


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    db, pf = new_ledger(tmp_path / "rev.sqlite3")
    yield db, pf
    db.close()


# ------------------------------------------------------------ 1. fee signs


def test_conservation_identity_with_every_fee_kind(ledger: Ledger) -> None:
    """Hand-computed: each fee is counted exactly once, with the documented sign."""
    db, pf = ledger
    buy = make_order(db, qty="10", limit="100.00")
    reserve(pf, db, buy, fee="0.50")
    pf.process_fill(fill(buy, "10", "100.00", fees="0.50"), now=NOW)  # cash -1000.50
    finish(pf, db, buy, OrderStatus.FILLED)
    sell = make_order(db, side=Side.SELL, qty="4", limit="110.00")
    pf.process_fill(fill(sell, "4", "110.00", fees="0.20"), now=NOW)  # cash +439.80
    pf.process_cash_activity(cash_activity(CashActivityKind.FEE, "-0.03", activity_id="f1"),
                             now=NOW)  # fmt: skip
    pf.process_cash_activity(cash_activity(CashActivityKind.DIVIDEND, "1.20", activity_id="d1"),
                             now=NOW)  # fmt: skip
    with db.read() as s:
        snap = pf.snapshot(s, now=NOW)
    b = snap.balances
    assert b.cash == D("5000") - D("1000.50") + D("439.80") - D("0.03") + D("1.20")
    # cost basis 1000.50; 4/10 removed = 400.20; realized = 439.80 - 400.20 = 39.60
    assert b.cost_basis_exposure == D("600.30")
    assert snap.realized_pnl == D("39.60")
    assert b.cash + b.cost_basis_exposure == (
        b.deposits + snap.realized_pnl + b.dividends + b.fee_entries
    )
    assert snap.fill_fees == D("0.70")
    assert snap.total_fees == D("0.73")  # 0.50 + 0.20 + 0.03, none double counted
    assert pf.check_invariants(now=NOW).ok


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"fees": D("-0.01")}, "non-negative"),
        ({"qty": D("0")}, "positive"),
        ({"price": D("-1")}, "positive"),
        ({"transaction_time": datetime(2026, 11, 9, 15)}, "timezone-aware"),  # noqa: DTZ001
    ],
)
def test_fill_sign_conventions_enforced(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "activity_id": "a", "client_order_id": "stb-x", "broker_order_id": "b",
        "symbol": "AAPL", "side": Side.BUY, "qty": D(1), "price": D(1), "fees": D(0),
        "transaction_time": NOW, "raw_json": "{}",
    }  # fmt: skip
    with pytest.raises(ValueError, match=message):
        ConfirmedFill(**{**base, **kwargs})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "amount"),
    [(CashActivityKind.FEE, "0.02"), (CashActivityKind.FEE, "0"),
     (CashActivityKind.DIVIDEND, "-1.00"), (CashActivityKind.DIVIDEND, "0")],
)  # fmt: skip
def test_cash_activity_sign_conventions_enforced(kind: CashActivityKind, amount: str) -> None:
    with pytest.raises(ValueError, match=r"FEE amount must be negative|DIVIDEND amount"):
        CashActivity("x", kind, D(amount), NOW, "{}")


def test_stored_sign_violation_is_detected(ledger: Ledger) -> None:
    db, pf = ledger
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
            "VALUES (1, '2026-11-09T15:00:00.000000+00:00', 'DIVIDEND', '-1', '2026-11-09', "
            "'2026-11-09')"
        )
    names = {v.invariant for v in pf.check_invariants(now=NOW).violations}
    assert "cash_entry_signs" in names


# ------------------------------------------- 2. unknown settlement durability


def test_unknown_settlement_stays_unavailable_across_restart_then_resolves_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unk.sqlite3"
    db, pf = new_ledger(path, with_calendar=False)
    buy = make_order(db, qty="10", limit="100.00")
    reserve(pf, db, buy)
    pf.process_fill(fill(buy, "10", "100.00"), now=NOW)
    finish(pf, db, buy, OrderStatus.FILLED)
    sell = make_order(db, side=Side.SELL, qty="10", limit="105.00")
    pf.process_fill(fill(sell, "10", "105.00"), now=NOW)
    db.close()
    for _ in range(2):  # survives restarts; time passing does not settle it
        db = Database.open(path, RuntimeMode.SIMULATE)
        pf = VirtualPortfolio(db, CONFIG)
        with db.read() as s:
            b = pf.snapshot(s, now=NOW + timedelta(days=7)).balances
        assert b.unsettled_proceeds == D("1050.00")
        assert b.buying_capacity == D("4000.00")  # 5050 cash - 1050 unsettled proceeds
        assert pf.resolve_settlements(now=NOW) == 0
        db.close()
    db = Database.open(path, RuntimeMode.SIMULATE)
    pf = VirtualPortfolio(db, CONFIG)
    seed_calendar(db, NOW)
    assert pf.resolve_settlements(now=NOW) == 2
    assert pf.resolve_settlements(now=NOW) == 0  # idempotent
    with db.read() as s:
        rows = s.fetch_all(
            "SELECT cash_entry_id, trade_date, settles_on, method, calendar_fetch_ids_json "
            "FROM settlement_resolutions ORDER BY id"
        )
    assert [(r[1], r[2], r[3]) for r in rows] == [("2026-11-09", "2026-11-10", "CALENDAR")] * 2
    assert all(json.loads(r[4]) for r in rows)  # names the calendar fetches used
    with pytest.raises(ConstraintViolationError), db.transaction() as tx:
        tx.execute("UPDATE settlement_resolutions SET settles_on = '2026-11-11'")
    db.close()


# --------------------------------------------- 3. booking vs execution order


def test_out_of_order_fill_is_booked_then_halts(ledger: Ledger) -> None:
    db, pf = ledger
    first = make_order(db, qty="10", limit="100.00")
    reserve(pf, db, first)
    pf.process_fill(fill(first, "10", "100.00", at=NOW), now=NOW)
    late = make_order(db, qty="5", limit="90.00")
    reserve(pf, db, late)
    earlier = NOW - timedelta(minutes=30)
    result = pf.process_fill(fill(late, "5", "90.00", at=earlier), now=NOW)
    assert result.outcome is FillOutcome.OUT_OF_ORDER
    assert result.halted and HaltReason.RECON_EXECUTION in result.halt_reasons
    with db.read() as s:
        qty = s.scalar("SELECT qty FROM positions WHERE symbol = 'AAPL'")
    assert qty == "15"  # recorded (broker truth) despite the halt
    other_symbol = make_order(db, qty="1", limit="10.00", symbol="MSFT")
    reserve(pf, db, other_symbol)
    ok = pf.process_fill(fill(other_symbol, "1", "10.00", at=earlier), now=NOW)
    assert ok.outcome is FillOutcome.APPLIED  # ordering is per symbol


# -------------------------------------------- baseline consistency property


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    qty=st.integers(min_value=1, max_value=30),
    buy=st.decimals(min_value=D("10.00"), max_value=D("150.00"), places=2),
    close_price=st.decimals(min_value=D("1.00"), max_value=D("300.00"), places=2),
)
def test_baseline_equals_cash_plus_quantity_times_official_close(
    qty: int, buy: Decimal, close_price: Decimal
) -> None:
    mon_day, tue = date(2026, 11, 9), date(2026, 11, 10)
    with tempfile.TemporaryDirectory() as tmp:
        db, pf = new_ledger(Path(tmp) / "b.sqlite3",
                            calendar_range=(date(2026, 10, 1), date(2026, 12, 31)))  # fmt: skip
        try:
            hold(db, pf, "AAPL", str(qty), str(buy), at=et(mon_day, 10))
            mon = LossLimitMonitor(db, pf, LL_CONFIG)
            close(mon, "AAPL", mon_day, str(close_price))
            r1 = evaluate_at(db, pf, mon, et(tue, 10), {"AAPL": str(close_price)})
            assert r1.daily_baseline is not None
            expected = D(5000) - D(qty) * buy + D(qty) * close_price
            assert r1.daily_baseline.equity == expected
            assert r1.daily_baseline.reference_session == mon_day
            # Re-evaluating later never recomputes or replaces the baseline.
            r2 = evaluate_at(db, pf, mon, et(tue, 11), {"AAPL": str(close_price)})
            assert r2.daily_baseline == r1.daily_baseline
            assert r1.daily is not None and r1.daily.loss == 0
        finally:
            db.close()
