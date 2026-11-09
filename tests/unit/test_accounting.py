"""Step 6: pure average-cost accounting math."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.accounting import (
    PositionState,
    apply_buy,
    apply_sell,
    average_price,
    buy_cost,
    sell_proceeds,
)

D = Decimal


def test_buy_then_partial_sells() -> None:
    p = apply_buy(PositionState.flat(), D(10), D("100.00"), D("0"))
    assert (p.qty, p.cost_basis, p.avg_cost) == (D(10), D("100.00") * 10, D("100.0000"))
    r = apply_sell(p, D(4), D("110.00"), D("0.02"))
    assert r.removed_cost == D("400.0000")
    assert r.realized_pnl == D("39.98")
    assert r.avg_cost_before == D("100.0000")
    assert r.position.avg_cost == D("100.0000")  # unchanged by a partial sell
    r2 = apply_sell(r.position, D(6), D("90.00"), D(0))
    assert r2.position == PositionState(D(0), D(0), D("-20.02"))
    assert r2.position.avg_cost is None


def test_full_close_removes_all_cost_without_residue() -> None:
    p = apply_buy(PositionState.flat(), D(3), D("100.01"), D("0.01"))  # 300.04
    first = apply_sell(p, D(1), D("100.01"), D(0))
    assert first.removed_cost == D("100.0133")
    last = apply_sell(first.position, D(2), D("100.01"), D(0))
    assert last.removed_cost == D("200.0267")
    assert last.position.cost_basis == D(0)
    assert first.removed_cost + last.removed_cost == D("300.04")


def test_oversell_is_recorded_not_rejected() -> None:
    p = apply_buy(PositionState.flat(), D(3), D("10.00"), D(0))
    r = apply_sell(p, D(5), D("10.00"), D(0))
    assert r.position.qty == D(-2)
    assert r.position.cost_basis == D(0)
    assert r.realized_pnl == D("20.00")
    short = apply_sell(r.position, D(1), D("10.00"), D(0))
    assert short.removed_cost == D(0)


def test_cash_helpers_and_average_price() -> None:
    assert buy_cost(D(3), D("33.3333"), D("0.0001")) == D("100.0000")
    assert sell_proceeds(D(3), D("33.3333"), D("0.0001")) == D("99.9998")
    assert average_price(D("1000"), D(3)) == D("333.3333")
    assert average_price(D("0"), D(0)) is None


prices = st.decimals(min_value=D("1.00"), max_value=D("2000.00"), places=2)
fees = st.decimals(min_value=D("0"), max_value=D("1.00"), places=2)
ops = st.lists(
    st.tuples(st.booleans(), st.integers(min_value=1, max_value=200), prices, fees),
    min_size=1,
    max_size=40,
)


@given(ops)
def test_cost_and_pnl_conservation(sequence: list[tuple[bool, int, Decimal, Decimal]]) -> None:
    """Σ buy costs == remaining cost + Σ removed; realized == Σ net proceeds - Σ removed;
    cash flow identity holds exactly for any sequence (including oversells)."""
    p = PositionState.flat()
    total_buy_cost = total_removed = total_net_proceeds = D(0)
    cash = D(0)
    for is_buy, qty, price, fee in sequence:
        q = D(qty)
        if is_buy:
            p = apply_buy(p, q, price, fee)
            total_buy_cost += buy_cost(q, price, fee)
            cash -= buy_cost(q, price, fee)
        else:
            r = apply_sell(p, q, price, fee)
            assert r.position.cost_basis >= 0
            p = r.position
            total_removed += r.removed_cost
            total_net_proceeds += sell_proceeds(q, price, fee)
            cash += sell_proceeds(q, price, fee)
    assert total_buy_cost == p.cost_basis + total_removed
    assert p.realized_pnl == total_net_proceeds - total_removed
    assert cash + p.cost_basis == p.realized_pnl


@given(ops)
def test_long_only_sequences_leave_no_cost_when_flat(
    sequence: list[tuple[bool, int, Decimal, Decimal]],
) -> None:
    p = PositionState.flat()
    for is_buy, qty, price, fee in sequence:
        q = D(qty)
        if is_buy:
            p = apply_buy(p, q, price, fee)
        elif p.qty > 0:
            p = apply_sell(p, min(q, p.qty), price, fee).position
        assert p.qty >= 0
        assert p.cost_basis >= 0
        if p.qty == 0:
            assert p.cost_basis == 0


@given(st.integers(min_value=2, max_value=500), prices, st.data())
def test_partial_sell_preserves_average_cost(qty: int, price: Decimal, data: st.DataObject) -> None:
    p = apply_buy(PositionState.flat(), D(qty), price, D(0))
    sold = data.draw(st.integers(min_value=1, max_value=qty - 1))
    r = apply_sell(p, D(sold), price, D(0))
    assert r.position.avg_cost == p.avg_cost
    assert abs(r.realized_pnl) <= D("0.0001")  # selling at cost: only rounding remains
