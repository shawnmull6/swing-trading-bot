"""Loader for ``config/fees.yaml`` (fee estimates used in pre-trade sizing)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from swing_trading_bot.config.yaml_loader import ConfigFileError, load_yaml_mapping
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.money import NonNegativeDecimal


class _FeeFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    buy_per_order: NonNegativeDecimal
    buy_per_share: NonNegativeDecimal
    sell_taf_per_share: NonNegativeDecimal
    sell_taf_max_per_order: NonNegativeDecimal
    sell_sec_per_notional: NonNegativeDecimal


def load_fee_model(path: Path) -> FeeModel:
    data = load_yaml_mapping(path)
    try:
        f = _FeeFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigFileError(f"{path}: {exc}") from None
    return FeeModel(
        buy_per_order=f.buy_per_order,
        buy_per_share=f.buy_per_share,
        sell_taf_per_share=f.sell_taf_per_share,
        sell_taf_max_per_order=f.sell_taf_max_per_order,
        sell_sec_per_notional=f.sell_sec_per_notional,
    )
