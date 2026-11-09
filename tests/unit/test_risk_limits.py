"""Step 2: risk limits file matches the approved decisions and validates strictly."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from swing_trading_bot.config.risk_limits import RiskLimits, load_risk_limits
from swing_trading_bot.config.yaml_loader import ConfigFileError

RISK_YAML = Path(__file__).resolve().parents[2] / "config" / "risk.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    data = yaml.safe_load(RISK_YAML.read_text())
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_repository_risk_file_matches_approved_decisions() -> None:
    r = load_risk_limits(RISK_YAML)
    # D14
    assert r.max_position_pct_equity == Decimal("0.20")
    assert r.max_open_positions == 4
    assert r.max_order_notional == Decimal("1000")
    assert r.planned_risk_pct_equity == Decimal("0.005")
    assert r.min_price == Decimal("10")
    assert r.max_sync_age_seconds == 120
    # U1 / U2
    assert r.loss_limits.max_daily_loss_pct == Decimal("0.02")
    assert r.loss_limits.daily_lockout_escalation_count == 2
    assert r.loss_limits.daily_lockout_escalation_sessions == 5
    assert r.loss_limits.max_weekly_drawdown_pct == Decimal("0.05")
    assert r.loss_limits.weekly_baseline == "calendar_week"
    # U3
    assert r.gap_stress.scenario_gap_pct == Decimal("0.15")
    assert r.gap_stress.max_position_scenario_loss_pct_equity == Decimal("0.03")
    assert r.gap_stress.max_portfolio_scenario_loss_pct_equity == Decimal("0.10")
    # U7 / D12
    assert r.mark_freshness.max_age_in_session_seconds == 900
    assert r.ambiguous_orders.min_absent_lookups >= 2


def test_full_position_fits_gap_scenario_exactly() -> None:
    """20% position x 15% gap = 3% of equity: at the per-position boundary (U3)."""
    r = load_risk_limits(RISK_YAML)
    scenario = r.max_position_pct_equity * r.gap_stress.scenario_gap_pct
    assert scenario == r.gap_stress.max_position_scenario_loss_pct_equity


def test_unquoted_decimal_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["max_position_pct_equity"] = 0.2  # a YAML float
    with pytest.raises(ConfigFileError, match="float"):
        load_risk_limits(_write(tmp_path, raw))


def test_unknown_key_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["max_leverage"] = "4"
    with pytest.raises(ConfigFileError, match="Extra inputs"):
        load_risk_limits(_write(tmp_path, raw))


def test_missing_key_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["min_price"]
    with pytest.raises(ConfigFileError, match="min_price"):
        load_risk_limits(_write(tmp_path, raw))


@pytest.mark.parametrize("value", ["0", "1.5", "-0.1"])
def test_fraction_bounds(tmp_path: Path, raw: dict[str, Any], value: str) -> None:
    raw["max_position_pct_equity"] = value
    with pytest.raises(ConfigFileError):
        load_risk_limits(_write(tmp_path, raw))


def test_single_absent_lookup_is_never_enough(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["ambiguous_orders"]["min_absent_lookups"] = 1
    with pytest.raises(ConfigFileError, match="single absent lookup"):
        load_risk_limits(_write(tmp_path, raw))


def test_escalation_window_must_cover_count(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["loss_limits"]["daily_lockout_escalation_sessions"] = 1
    with pytest.raises(ConfigFileError):
        load_risk_limits(_write(tmp_path, raw))


def test_position_scenario_cannot_exceed_portfolio(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["gap_stress"]["max_position_scenario_loss_pct_equity"] = "0.2"
    with pytest.raises(ConfigFileError, match="per-position"):
        load_risk_limits(_write(tmp_path, raw))


def test_planned_risk_below_daily_loss(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["planned_risk_pct_equity"] = "0.03"
    with pytest.raises(ConfigFileError, match="planned risk"):
        load_risk_limits(_write(tmp_path, raw))


def test_boolean_is_not_an_integer_count(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["max_open_positions"] = True
    with pytest.raises(ConfigFileError):
        load_risk_limits(_write(tmp_path, raw))


def test_non_mapping_file(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ConfigFileError, match="mapping"):
        load_risk_limits(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="not found"):
        load_risk_limits(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_text("a: [unclosed\n")
    with pytest.raises(ConfigFileError, match="invalid YAML"):
        load_risk_limits(path)


def test_model_is_frozen() -> None:
    r = load_risk_limits(RISK_YAML)
    with pytest.raises(ValueError, match="frozen"):
        r.max_open_positions = 10  # type: ignore[misc]
    assert isinstance(r, RiskLimits)
