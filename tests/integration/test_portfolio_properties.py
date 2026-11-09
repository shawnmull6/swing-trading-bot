"""Step 6: property-based accounting conservation through the real service + SQLite."""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import OrderStatus, Side
from swing_trading_bot.ledger.portfolio import (
    CashActivityKind,
    FillOutcome,
    InsufficientBuyingCapacityError,
)
from swing_trading_bot.ledger.repositories import PositionsRepository
from tests.support.portfolio import (
    NOW,
    cash_activity,
    fill,
    finish,
    make_order,
    new_ledger,
    reserve,
)

D = Decimal
SYMBOLS = ("AAPL", "MSFT", "SPY")

operation = st.one_of(
    st.tuples(st.just("buy"), st.sampled_from(SYMBOLS), st.integers(1, 15),
              st.decimals(min_value=D("10.00"), max_value=D("400.00"), places=2),
              st.integers(0, 15)),  # fill qty fraction (out of 15)
    st.tuples(st.just("sell"), st.sampled_from(SYMBOLS), st.integers(1, 15),
              st.decimals(min_value=D("5.00"), max_value=D("500.00"), places=2),
              st.integers(0, 3)),  # fee cents
    st.tuples(st.just("dividend"), st.just("AAPL"), st.integers(1, 500), st.just(D(0)), st.just(0)),
    st.tuples(st.just("fee"), st.just("AAPL"), st.integers(1, 50), st.just(D(0)), st.just(0)),
)  # fmt: skip


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(operation, min_size=1, max_size=12))
def test_accounting_conservation_through_service(
    ops: list[tuple[str, str, int, Decimal, int]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db, pf = new_ledger(
            Path(tmp) / "prop.sqlite3", calendar_range=(date(2026, 10, 1), date(2026, 12, 31))
        )
        try:
            for i, (kind, symbol, n, price, extra) in enumerate(ops):
                if kind == "buy":
                    order = make_order(db, qty=str(n), limit=str(price), symbol=symbol)
                    try:
                        reserve(pf, db, order)
                    except InsufficientBuyingCapacityError:
                        continue
                    filled = (n * extra) // 15
                    if filled:
                        r = pf.process_fill(fill(order, str(filled), str(price)), now=NOW)
                        assert r.outcome is FillOutcome.APPLIED and not r.halted, r
                    finish(pf, db, order,
                           OrderStatus.FILLED if filled == n else OrderStatus.CANCELED)  # fmt: skip
                elif kind == "sell":
                    with db.read() as s:
                        pos = PositionsRepository().get(s, 1, symbol)
                    held = int(pos.qty) if pos is not None else 0
                    qty = min(n, held)
                    if qty == 0:
                        continue
                    order = make_order(db, side=Side.SELL, qty=str(qty), limit=str(price),
                                       symbol=symbol)  # fmt: skip
                    r = pf.process_fill(
                        fill(order, str(qty), str(price), fees=f"0.0{extra}"), now=NOW
                    )
                    assert not r.halted, r
                else:
                    amount = D(n) / 100
                    r = pf.process_cash_activity(
                        cash_activity(
                            CashActivityKind.DIVIDEND
                            if kind == "dividend"
                            else CashActivityKind.FEE,
                            str(amount if kind == "dividend" else -amount),
                            activity_id=f"{kind}-{i}",
                        ),
                        now=NOW,
                    )
                    assert not r.halted, r
            report = pf.check_invariants(now=NOW)
            assert report.ok, report.violations
            with db.read() as s:
                snap = pf.snapshot(s, now=NOW)
            b = snap.balances
            assert b.cash >= 0
            assert b.available_cash >= 0
            assert b.deployed_capital <= b.max_capital_allocation
            assert b.buying_capacity == max(D(0), min(b.available_cash, b.capital_headroom))
            assert b.cash + b.cost_basis_exposure == (
                b.deposits + snap.realized_pnl + b.dividends + b.fee_entries
            )
        finally:
            db.close()
