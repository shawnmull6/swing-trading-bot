"""Pure position-sizing math for long limit entries (M1 step 11, ARCHITECTURE §12.14).

Extracted unchanged from :mod:`swing_trading_bot.risk.pretrade` (Step 16.14) so offline
research code (the strategy backtest) can size with exactly the same constraints without
importing the ledger. ``pretrade`` re-exports every name; the engine still owns gating,
evidence and the decision. No I/O here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from swing_trading_bot.config.risk_limits import RiskLimits
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, ZERO, reservation_amount


class Constraint(StrEnum):
    RISK_BUDGET = "RISK_BUDGET"
    POSITION_CONCENTRATION = "POSITION_CONCENTRATION"
    ORDER_COMMITMENT = "ORDER_COMMITMENT"
    CAPITAL_CEILING = "CAPITAL_CEILING"
    AVAILABLE_CASH = "AVAILABLE_CASH"
    GAP_STRESS_POSITION = "GAP_STRESS_POSITION"
    GAP_STRESS_PORTFOLIO = "GAP_STRESS_PORTFOLIO"
    BROKER_CASH_BACKSTOP = "BROKER_CASH_BACKSTOP"


@dataclass(frozen=True, slots=True)
class ConstraintCap:
    constraint: Constraint
    max_qty: int
    limit: Decimal
    used: Decimal
    """Amount already consumed before this order (e.g. existing exposure)."""


@dataclass(frozen=True, slots=True)
class SizingPreview:
    max_qty: int
    caps: tuple[ConstraintCap, ...]
    risk_per_share: Decimal

    @property
    def binding(self) -> tuple[Constraint, ...]:
        """The constraints that set ``max_qty`` (the limiting constraints)."""
        return tuple(c.constraint for c in self.caps if c.max_qty == self.max_qty)

    def cap(self, constraint: Constraint) -> ConstraintCap:
        return next(c for c in self.caps if c.constraint is constraint)


# ------------------------------------------------------------------- sizing


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
    if denominator <= ZERO or numerator <= ZERO:
        return 0
    return int(DECIMAL_CONTEXT.divide(numerator, denominator).to_integral_value(ROUND_FLOOR))


def max_whole_qty(ok: Callable[[Decimal], bool], estimate: int) -> int:
    """Largest whole q >= 0 with ok(q) for a monotone (non-increasing truth) predicate.
    Starts at ``estimate`` and only ever moves to verified values; never rounds up an
    unsafe quantity."""
    q = max(estimate, 0)
    while q > 0 and not ok(Decimal(q)):
        q -= 1
    for _ in range(1000):
        if not ok(Decimal(q + 1)):
            return q
        q += 1
    return q  # pragma: no cover - estimates are within a few shares


@dataclass(frozen=True, slots=True)
class SizingInputs:
    entry: Decimal
    stop: Decimal
    equity: Decimal
    baseline_equity: Decimal
    held_market_value: Decimal
    pending_symbol_commitment: Decimal
    capital_headroom: Decimal
    available_cash: Decimal
    portfolio_stressed: Decimal
    broker_cash: Decimal


def size(inputs: SizingInputs, limits: RiskLimits, fees: FeeModel) -> SizingPreview:
    i = inputs
    rps = i.entry - i.stop
    gap = limits.gap_stress.scenario_gap_pct

    def reserve(q: Decimal) -> Decimal:
        return reservation_amount(q * i.entry + fees.buy_fee(q))

    risk_budget = limits.planned_risk_pct_equity * i.equity
    conc_limit = limits.max_position_pct_equity * i.equity
    exposure = i.held_market_value + i.pending_symbol_commitment
    pos_stress_limit = limits.gap_stress.max_position_scenario_loss_pct_equity * i.baseline_equity
    port_stress_limit = limits.gap_stress.max_portfolio_scenario_loss_pct_equity * i.baseline_equity
    specs: list[tuple[Constraint, Decimal, Decimal, Callable[[Decimal], bool], int]] = [
        (Constraint.RISK_BUDGET, risk_budget, ZERO,
         lambda q: q * rps + fees.buy_fee(q) + fees.sell_fee(q, i.stop) <= risk_budget,
         _floor_div(risk_budget, rps)),
        (Constraint.POSITION_CONCENTRATION, conc_limit, exposure,
         lambda q: exposure + q * i.entry <= conc_limit,
         _floor_div(conc_limit - exposure, i.entry)),
        (Constraint.ORDER_COMMITMENT, limits.max_order_notional, ZERO,
         lambda q: reserve(q) <= limits.max_order_notional,
         _floor_div(limits.max_order_notional, i.entry)),
        (Constraint.CAPITAL_CEILING, i.capital_headroom, ZERO,
         lambda q: reserve(q) <= i.capital_headroom,
         _floor_div(i.capital_headroom, i.entry)),
        (Constraint.AVAILABLE_CASH, i.available_cash, ZERO,
         lambda q: reserve(q) <= i.available_cash,
         _floor_div(i.available_cash, i.entry)),
        (Constraint.GAP_STRESS_POSITION, pos_stress_limit, exposure * gap,
         lambda q: (exposure + q * i.entry) * gap <= pos_stress_limit,
         _floor_div(pos_stress_limit - exposure * gap, i.entry * gap)),
        (Constraint.GAP_STRESS_PORTFOLIO, port_stress_limit, i.portfolio_stressed,
         lambda q: i.portfolio_stressed + q * i.entry * gap <= port_stress_limit,
         _floor_div(port_stress_limit - i.portfolio_stressed, i.entry * gap)),
        (Constraint.BROKER_CASH_BACKSTOP, i.broker_cash, ZERO,
         lambda q: q * i.entry + fees.buy_fee(q) <= i.broker_cash,
         _floor_div(i.broker_cash, i.entry)),
    ]  # fmt: skip
    caps = tuple(
        ConstraintCap(name, max_whole_qty(ok, estimate), limit, used)
        for name, limit, used, ok, estimate in specs
    )
    return SizingPreview(max_qty=min(c.max_qty for c in caps), caps=caps, risk_per_share=rps)


__all__ = [
    "Constraint",
    "ConstraintCap",
    "SizingInputs",
    "SizingPreview",
    "max_whole_qty",
    "size",
]
