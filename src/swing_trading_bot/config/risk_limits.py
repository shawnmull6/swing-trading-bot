"""Risk limits (ARCHITECTURE §11, decisions D14 and U1-U3, U7).

Loaded from ``config/risk.yaml``. Percentages are fractions of *current virtual
equity* (D8). Every field is required; there are no silent defaults for limits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from swing_trading_bot.config.yaml_loader import ConfigFileError, load_yaml_mapping
from swing_trading_bot.domain.money import Fraction, PositiveDecimal

PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GapStressLimits(_Strict):
    """A *scenario*, not a maximum possible loss (U3, ARCHITECTURE §11.3)."""

    scenario_gap_pct: Fraction
    max_position_scenario_loss_pct_equity: Fraction
    max_portfolio_scenario_loss_pct_equity: Fraction

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_position_scenario_loss_pct_equity > self.max_portfolio_scenario_loss_pct_equity:
            raise ValueError("per-position scenario limit cannot exceed the portfolio limit")
        return self


class LossLimits(_Strict):
    max_daily_loss_pct: Fraction
    daily_lockout_escalation_count: PositiveInt
    daily_lockout_escalation_sessions: PositiveInt
    max_weekly_drawdown_pct: Fraction
    weekly_baseline: str = Field(pattern=r"^calendar_week$")

    @model_validator(mode="after")
    def _escalation(self) -> Self:
        if self.daily_lockout_escalation_count < 2:
            raise ValueError("escalation count must be at least 2")
        if self.daily_lockout_escalation_sessions < self.daily_lockout_escalation_count:
            raise ValueError("escalation window must be >= escalation count")
        return self


class MarkFreshness(_Strict):
    max_age_in_session_seconds: PositiveInt
    max_future_skew_seconds: PositiveInt
    max_cross_check_divergence_pct: Fraction


class AmbiguousOrderPolicy(_Strict):
    min_absent_lookups: PositiveInt
    min_absent_window_seconds: PositiveInt
    list_scan_lookback_seconds: PositiveInt
    escalate_after_seconds: PositiveInt

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.min_absent_lookups < 2:
            raise ValueError("a single absent lookup is never sufficient (D12)")
        if self.escalate_after_seconds <= self.min_absent_window_seconds:
            raise ValueError("escalation must come after the absence evidence window")
        return self


class ReconciliationPolicy(_Strict):
    max_snapshot_retries: PositiveInt
    max_consecutive_inconclusive: PositiveInt


class BrokerOutagePolicy(_Strict):
    """§10.5 circuit breaker (approved step 14.1). Not a trading risk limit."""

    min_outage_seconds: PositiveInt = 1800
    min_failed_cycles: PositiveInt = 3
    min_cycle_spacing_seconds: PositiveInt = 60


class RiskLimits(_Strict):
    max_position_pct_equity: Fraction
    max_open_positions: PositiveInt
    max_order_notional: PositiveDecimal
    planned_risk_pct_equity: Fraction
    min_price: PositiveDecimal
    max_sync_age_seconds: PositiveInt
    gap_stress: GapStressLimits
    loss_limits: LossLimits
    mark_freshness: MarkFreshness
    ambiguous_orders: AmbiguousOrderPolicy
    reconciliation: ReconciliationPolicy
    broker_outage: BrokerOutagePolicy = BrokerOutagePolicy()

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.planned_risk_pct_equity >= self.loss_limits.max_daily_loss_pct:
            raise ValueError("planned risk per trade must be below the daily loss limit")
        return self


def load_risk_limits(path: Path) -> RiskLimits:
    data = load_yaml_mapping(path)
    try:
        return RiskLimits.model_validate(data)
    except ValidationError as exc:
        raise ConfigFileError(f"{path}: {exc}") from None
