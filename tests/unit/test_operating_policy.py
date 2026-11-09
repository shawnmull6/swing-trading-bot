"""Step 2: virtual operating policy and M1 hard constraints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from swing_trading_bot.config.operating_policy import load_operating_policy
from swing_trading_bot.config.yaml_loader import ConfigFileError
from swing_trading_bot.domain.enums import AccountType, InstrumentType, OrderType

POLICY_YAML = Path(__file__).resolve().parents[2] / "config" / "operating_policy.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    data = yaml.safe_load(POLICY_YAML.read_text())
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_repository_policy_is_conservative_cash_profile() -> None:
    policy = load_operating_policy(POLICY_YAML)
    p = policy.profile
    assert policy.active_profile == "cash_conservative"
    assert p.account_type is AccountType.CASH
    assert p.settlement.settlement_cycle_days == 1
    assert p.settlement.buys_require_settled_cash is True
    assert p.settlement.calendar_source == "alpaca"
    assert p.max_leverage == 1
    assert p.allow_short is False
    assert p.allow_fractional is False
    assert p.allow_extended_hours is False
    assert p.buy_order_types == {OrderType.LIMIT}
    assert p.allowed_instrument_types == {InstrumentType.EQUITY, InstrumentType.ETF}
    assert p.allow_same_day_round_trip is False  # D6/D7
    assert p.allow_same_day_risk_exit is True  # U5
    assert p.pdt_emulation is False  # PDT not assumed for cash accounts


def test_margin_profile_is_reserved(tmp_path: Path, raw: dict[str, Any]) -> None:
    assert raw["profiles"]["margin_small"]["pdt_emulation"] is True
    raw["active_profile"] = "margin_small"
    with pytest.raises(ConfigFileError, match="reserved"):
        load_operating_policy(_write(tmp_path, raw))


def test_undefined_active_profile(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["active_profile"] = "nope"
    with pytest.raises(ConfigFileError, match="not defined"):
        load_operating_policy(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("max_leverage", "2", "max_leverage must be 1"),
        ("allow_short", True, "shorting"),
        ("allow_fractional", True, "fractional"),
        ("allow_extended_hours", True, "extended hours"),
        ("buy_order_types", ["LIMIT", "MARKET"], "LIMIT orders only"),
    ],
)
def test_m1_constraints_cannot_be_relaxed(
    tmp_path: Path, raw: dict[str, Any], key: str, value: object, message: str
) -> None:
    raw["profiles"]["cash_conservative"][key] = value
    with pytest.raises(ConfigFileError, match=message):
        load_operating_policy(_write(tmp_path, raw))


def test_calendar_settings_and_t_plus_one_only(tmp_path: Path, raw: dict[str, Any]) -> None:
    s = load_operating_policy(POLICY_YAML).profile.settlement
    assert s.min_calendar_coverage_days == 10
    assert s.calendar_max_age_hours == 36
    assert s.calendar_horizon_warning_days == 365
    raw["profiles"]["cash_conservative"]["settlement"]["settlement_cycle_days"] = 2
    with pytest.raises(ConfigFileError, match="T\\+1"):
        load_operating_policy(_write(tmp_path, raw))


def test_unsettled_buys_cannot_be_enabled(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["profiles"]["cash_conservative"]["settlement"]["buys_require_settled_cash"] = False
    with pytest.raises(ConfigFileError, match="settled cash"):
        load_operating_policy(_write(tmp_path, raw))


def test_unknown_instrument_type_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["profiles"]["cash_conservative"]["allowed_instrument_types"] = ["EQUITY", "OPTION"]
    with pytest.raises(ConfigFileError):
        load_operating_policy(_write(tmp_path, raw))


def test_unknown_key_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["profiles"]["cash_conservative"]["use_broker_buying_power"] = True
    with pytest.raises(ConfigFileError, match="Extra inputs"):
        load_operating_policy(_write(tmp_path, raw))
