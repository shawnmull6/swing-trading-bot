"""Virtual operating policy / account profiles (ARCHITECTURE §6.3, D6/D7, U5).

The policy describes how the *virtual* portfolio behaves, independent of the actual
Alpaca paper account (a 4x margin account). Account-type rules live in profiles so
they stay configurable; M1 additionally enforces hard constraints that no profile can
relax (no leverage, no shorting, whole shares, limit-only buys, regular hours).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from swing_trading_bot.config.yaml_loader import ConfigFileError, load_yaml_mapping
from swing_trading_bot.domain.enums import AccountType, AssetClass, InstrumentType, OrderType
from swing_trading_bot.domain.money import ONE, PositiveDecimal


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SettlementPolicy(_Strict):
    settlement_cycle_days: int = Field(strict=True, ge=1, le=3)
    calendar_source: Literal["alpaca"]
    buys_require_settled_cash: bool
    proceeds_available_at: Literal["start_of_settlement_date_market_tz"]
    min_calendar_coverage_days: int = Field(strict=True, ge=1)
    calendar_max_age_hours: int = Field(strict=True, ge=1, le=168)
    calendar_horizon_warning_days: int = Field(strict=True, ge=1)


class AccountProfile(_Strict):
    account_type: AccountType
    selectable: bool
    description: str
    market_timezone: Literal["America/New_York"]
    settlement: SettlementPolicy
    max_leverage: PositiveDecimal
    allow_short: bool
    allow_fractional: bool
    allow_extended_hours: bool
    allowed_asset_classes: frozenset[AssetClass]
    allowed_instrument_types: frozenset[InstrumentType]
    buy_order_types: frozenset[OrderType]
    allow_same_day_round_trip: bool
    allow_same_day_risk_exit: bool
    pdt_emulation: bool


class OperatingPolicy(_Strict):
    active_profile: str
    profiles: dict[str, AccountProfile]

    @model_validator(mode="after")
    def _active_is_valid(self) -> Self:
        profile = self.profiles.get(self.active_profile)
        if profile is None:
            raise ValueError(f"active_profile {self.active_profile!r} is not defined")
        if not profile.selectable:
            raise ValueError(f"profile {self.active_profile!r} is reserved and not selectable")
        _enforce_m1_constraints(self.active_profile, profile)
        return self

    @property
    def profile(self) -> AccountProfile:
        return self.profiles[self.active_profile]


def _enforce_m1_constraints(name: str, profile: AccountProfile) -> None:
    problems: list[str] = []
    if profile.max_leverage != ONE:
        problems.append("max_leverage must be 1")
    if profile.allow_short:
        problems.append("shorting is not permitted")
    if profile.allow_fractional:
        problems.append("fractional shares are not permitted")
    if profile.allow_extended_hours:
        problems.append("extended hours are not permitted")
    if profile.buy_order_types != frozenset({OrderType.LIMIT}):
        problems.append("buys must be LIMIT orders only")
    if profile.allowed_asset_classes != frozenset({AssetClass.US_EQUITY}):
        problems.append("only US equities are permitted")
    if not profile.allowed_instrument_types <= {InstrumentType.EQUITY, InstrumentType.ETF}:
        problems.append("only equities and ETFs are permitted")
    if not profile.settlement.buys_require_settled_cash:
        problems.append("buys must require settled cash")
    if profile.settlement.settlement_cycle_days != 1:
        problems.append("settlement cycle must be T+1 (calendar logic implements T+1 only)")
    if problems:
        raise ValueError(f"profile {name!r} violates M1 constraints: " + "; ".join(problems))


def load_operating_policy(path: Path) -> OperatingPolicy:
    data = load_yaml_mapping(path)
    try:
        return OperatingPolicy.model_validate(data)
    except ValidationError as exc:
        raise ConfigFileError(f"{path}: {exc}") from None
