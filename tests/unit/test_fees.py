"""Step 11: conservative fee estimates (rounded up, capped TAF, never negative)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.config.fees import load_fee_model
from swing_trading_bot.config.yaml_loader import ConfigFileError
from swing_trading_bot.domain.fees import FeeModel

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


def test_shipped_estimates() -> None:
    fees = load_fee_model(ROOT / "config" / "fees.yaml")
    assert fees.buy_fee(D(100)) == 0
    assert fees.sell_fee(D(0), D(50)) == 0
    # 12 shares at $48: TAF 0.002 -> 0.01; SEC 0.0160 -> 0.02
    assert fees.sell_fee(D(12), D(48)) == D("0.03")
    # TAF is capped per order
    assert fees.sell_fee(D(1_000_000), D(10)) == D("8.30") + D("278.00")


def test_buy_fee_rounds_up_and_negative_rates_are_rejected() -> None:
    fees = FeeModel(D("1.00"), D("0.005"), D(0), D(0), D(0))
    assert fees.buy_fee(D(3)) == D("1.02")  # 1.015 rounded up
    assert fees.buy_fee(D(0)) == 0
    with pytest.raises(ValueError, match="buy_per_share"):
        FeeModel(D(0), D("-0.01"), D(0), D(0), D(0))


def test_loader_rejects_unknown_keys(tmp_path: Path) -> None:
    bad = tmp_path / "fees.yaml"
    bad.write_text((ROOT / "config" / "fees.yaml").read_text() + "\nsurprise: '1'\n")
    with pytest.raises(ConfigFileError):
        load_fee_model(bad)
