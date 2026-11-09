"""Pure average-cost position accounting (ARCHITECTURE §6.1, D5).

Rules
-----
* A position carries ``qty`` and total ``cost_basis``; average cost is derived
  (``cost_basis / qty``) and never stored, so it cannot drift.
* Buy: ``cost_basis += qty * price + fees``.
* Sell: the cost removed is proportional, ``cost_basis * sold / held``, quantized to
  0.0001 (half-even). Selling the whole position removes the entire remaining cost
  basis, so no residue is ever left behind. ``realized = qty * price - fees -
  removed_cost``.
* Conservation holds exactly by construction:
  ``removed_cost + remaining_cost == cost_basis_before`` and therefore
  ``cash + Σ cost_basis == initial capital + Σ realized + dividends + fee entries``.
* Broker truth is never rejected: selling more than is held (an invariant breach)
  removes all remaining cost and leaves a negative quantity for the invariant checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from swing_trading_bot.domain.money import (
    CASH_QUANTUM,
    DECIMAL_CONTEXT,
    PRICE_QUANTUM,
    ZERO,
)


@dataclass(frozen=True, slots=True)
class PositionState:
    qty: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal

    @classmethod
    def flat(cls) -> PositionState:
        return cls(ZERO, ZERO, ZERO)

    @property
    def avg_cost(self) -> Decimal | None:
        """Average cost per share at price precision; None when flat or short."""
        if self.qty <= ZERO:
            return None
        return DECIMAL_CONTEXT.divide(self.cost_basis, self.qty).quantize(
            PRICE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
        )


@dataclass(frozen=True, slots=True)
class SellResult:
    position: PositionState
    removed_cost: Decimal
    realized_pnl: Decimal
    avg_cost_before: Decimal | None


def buy_cost(qty: Decimal, price: Decimal, fees: Decimal) -> Decimal:
    """Cash paid for a buy fill (positive number). Exact: qty is whole, price <= 4dp."""
    return qty * price + fees


def sell_proceeds(qty: Decimal, price: Decimal, fees: Decimal) -> Decimal:
    """Net cash received for a sell fill (may be negative only in pathological cases)."""
    return qty * price - fees


def apply_buy(pos: PositionState, qty: Decimal, price: Decimal, fees: Decimal) -> PositionState:
    return PositionState(
        qty=pos.qty + qty,
        cost_basis=pos.cost_basis + buy_cost(qty, price, fees),
        realized_pnl=pos.realized_pnl,
    )


def apply_sell(pos: PositionState, qty: Decimal, price: Decimal, fees: Decimal) -> SellResult:
    avg_before = pos.avg_cost
    if pos.qty <= ZERO or qty >= pos.qty:
        removed = pos.cost_basis  # full close (or oversell): remove everything left
    else:
        removed = DECIMAL_CONTEXT.divide(pos.cost_basis * qty, pos.qty).quantize(
            CASH_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
        )
    realized = sell_proceeds(qty, price, fees) - removed
    return SellResult(
        position=PositionState(
            qty=pos.qty - qty,
            cost_basis=pos.cost_basis - removed,
            realized_pnl=pos.realized_pnl + realized,
        ),
        removed_cost=removed,
        realized_pnl=realized,
        avg_cost_before=avg_before,
    )


def average_price(notional: Decimal, qty: Decimal) -> Decimal | None:
    """Average fill price from exact totals (sum of qty*price over sum of qty)."""
    if qty <= ZERO:
        return None
    return DECIMAL_CONTEXT.divide(notional, qty).quantize(
        PRICE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
    )
