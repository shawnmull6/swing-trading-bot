"""Loss-limit and gap-stress arithmetic (ARCHITECTURE §11.2-11.3, U1-U3). Pure logic.

* Every threshold is measured against a **recorded baseline equity**, never against
  current equity, so the denominator does not move while losses accumulate.
* Breach tests are exact Decimal comparisons without division:
  ``baseline - equity >= pct * baseline``. Percentages are only computed for reporting.
* The 15% gap stress is a **hypothetical scenario**, not a maximum possible loss: a
  stock can gap far more than 15%, and a long position can lose 100%.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from swing_trading_bot.domain.money import DECIMAL_CONTEXT, ZERO

PCT_QUANTUM = Decimal("0.000001")


def loss_amount(baseline: Decimal, equity: Decimal) -> Decimal:
    """Positive when equity is below the baseline."""
    return baseline - equity


def loss_fraction(baseline: Decimal, equity: Decimal) -> Decimal:
    """Reporting only: (baseline - equity) / baseline at 1e-6 precision."""
    if baseline <= ZERO:
        raise ValueError("baseline equity must be positive")
    return DECIMAL_CONTEXT.divide(loss_amount(baseline, equity), baseline).quantize(
        PCT_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
    )


def loss_budget(baseline: Decimal, threshold: Decimal) -> Decimal:
    return threshold * baseline


def is_breached(baseline: Decimal, equity: Decimal, threshold: Decimal) -> bool:
    """True when the loss reaches or exceeds ``threshold`` x baseline (inclusive)."""
    if baseline <= ZERO:
        raise ValueError("baseline equity must be positive")
    return loss_amount(baseline, equity) >= loss_budget(baseline, threshold)


@dataclass(frozen=True, slots=True)
class LimitMeasure:
    baseline: Decimal
    equity: Decimal
    threshold: Decimal

    @property
    def loss(self) -> Decimal:
        return loss_amount(self.baseline, self.equity)

    @property
    def loss_pct(self) -> Decimal:
        return loss_fraction(self.baseline, self.equity)

    @property
    def budget(self) -> Decimal:
        return loss_budget(self.baseline, self.threshold)

    @property
    def remaining(self) -> Decimal:
        """Loss still allowed before the threshold, never below zero."""
        return max(ZERO, self.budget - self.loss)

    @property
    def breached(self) -> bool:
        return is_breached(self.baseline, self.equity, self.threshold)


# ------------------------------------------------------------------- gap stress


@dataclass(frozen=True, slots=True)
class PositionStress:
    symbol: str
    market_value: Decimal
    stressed_loss: Decimal
    limit: Decimal

    @property
    def exceeded(self) -> bool:
        return self.stressed_loss > self.limit

    @property
    def headroom(self) -> Decimal:
        return self.limit - self.stressed_loss


@dataclass(frozen=True, slots=True)
class GapStressReport:
    """Losses under one hypothetical adverse gap. NOT a bound on actual loss."""

    scenario_pct: Decimal
    denominator: Decimal
    positions: tuple[PositionStress, ...]
    commitments_stressed: Decimal
    portfolio_stressed: Decimal
    portfolio_limit: Decimal

    @property
    def portfolio_exceeded(self) -> bool:
        return self.portfolio_stressed > self.portfolio_limit

    @property
    def portfolio_headroom(self) -> Decimal:
        return self.portfolio_limit - self.portfolio_stressed

    @property
    def positions_exceeded(self) -> tuple[str, ...]:
        return tuple(p.symbol for p in self.positions if p.exceeded)


def gap_stress(
    market_values: Mapping[str, Decimal],
    commitments: Decimal,
    *,
    denominator: Decimal,
    scenario_pct: Decimal,
    position_limit_pct: Decimal,
    portfolio_limit_pct: Decimal,
) -> GapStressReport:
    """Apply an adverse ``scenario_pct`` move to every position (and to working buy
    commitments, which may become positions). Limits are fractions of ``denominator``
    (the recorded session-baseline equity)."""
    if denominator <= ZERO:
        raise ValueError("stress denominator must be positive")
    position_limit = position_limit_pct * denominator
    positions = tuple(
        PositionStress(sym, mv, abs(mv) * scenario_pct, position_limit)
        for sym, mv in sorted(market_values.items())
    )
    commitments_stressed = commitments * scenario_pct
    total = sum((p.stressed_loss for p in positions), ZERO) + commitments_stressed
    return GapStressReport(
        scenario_pct=scenario_pct,
        denominator=denominator,
        positions=positions,
        commitments_stressed=commitments_stressed,
        portfolio_stressed=total,
        portfolio_limit=portfolio_limit_pct * denominator,
    )


@dataclass(frozen=True, slots=True)
class GapObservation:
    """Move from the reference session's official close to the first valid new price."""

    symbol: str
    qty_at_close: Decimal
    prior_close: Decimal
    price: Decimal

    @property
    def pnl(self) -> Decimal:
        return self.qty_at_close * (self.price - self.prior_close)

    @property
    def pct(self) -> Decimal:
        return DECIMAL_CONTEXT.divide(self.price - self.prior_close, self.prior_close).quantize(
            PCT_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
        )
