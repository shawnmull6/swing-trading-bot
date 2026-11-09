"""Step 2: process settings. All values here are fake test fixtures."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.config.settings import (
    ENV_TO_FIELD,
    Settings,
    SettingsError,
    load_settings,
    settings_from_mapping,
)

FAKE_KEY = "PKFAKETESTKEY000000001"
FAKE_SECRET = "fakeTestSecretValue/DoNotUse/0000000000001"


def _env(**overrides: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "ALPACA_API_KEY": FAKE_KEY,
        "ALPACA_SECRET_KEY": FAKE_SECRET,
        "TRADING_MODE": "paper",
        "TRADING_ENABLED": "false",
        "INITIAL_CAPITAL": "5000",
        "MAX_CAPITAL_ALLOCATION": "5000",
    }
    base.update(overrides)
    return base


def test_valid_settings() -> None:
    s = settings_from_mapping(_env())
    assert s.trading_mode == "paper"
    assert s.trading_enabled is False
    assert s.initial_capital == Decimal("5000")
    assert s.max_capital_allocation == Decimal("5000")


def test_defaults_are_safe() -> None:
    s = settings_from_mapping(
        _env(TRADING_ENABLED=None, INITIAL_CAPITAL=None, MAX_CAPITAL_ALLOCATION=None)
    )
    assert s.trading_enabled is False
    assert s.initial_capital == Decimal("5000")
    assert s.max_capital_allocation == Decimal("5000")


@pytest.mark.parametrize("mode", ["live", "LIVE", "Paper", "production", "paper "])
def test_non_paper_mode_rejected(mode: str) -> None:
    with pytest.raises(SettingsError, match="TRADING_MODE"):
        settings_from_mapping(_env(TRADING_MODE=mode))


def test_trading_mode_is_required() -> None:
    with pytest.raises(SettingsError, match="TRADING_MODE: required"):
        settings_from_mapping(_env(TRADING_MODE=None))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False)],
)
def test_trading_enabled_accepts_unambiguous_values(raw: str, expected: bool) -> None:
    assert settings_from_mapping(_env(TRADING_ENABLED=raw)).trading_enabled is expected


@pytest.mark.parametrize("raw", ["yes", "no", "on", "enabled", "2", "t"])
def test_trading_enabled_rejects_ambiguous_values(raw: str) -> None:
    with pytest.raises(SettingsError, match="TRADING_ENABLED"):
        settings_from_mapping(_env(TRADING_ENABLED=raw))


@pytest.mark.parametrize("raw", ["0", "-1", "$5,000", "5000.001", "abc", "NaN", "Infinity"])
def test_capital_validation(raw: str) -> None:
    with pytest.raises(SettingsError, match="INITIAL_CAPITAL"):
        settings_from_mapping(_env(INITIAL_CAPITAL=raw))


@pytest.mark.parametrize("missing", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"])
def test_missing_credentials_reported_by_name(missing: str) -> None:
    with pytest.raises(SettingsError, match=f"{missing}: required"):
        settings_from_mapping(_env(**{missing: None}))


def test_secrets_never_appear_in_repr_str_or_dump() -> None:
    s = settings_from_mapping(_env())
    for text in (repr(s), str(s), s.model_dump_json(), str(s.model_dump()), str(s.describe())):
        assert FAKE_KEY not in text
        assert FAKE_SECRET not in text


def test_validation_errors_never_contain_secret_values() -> None:
    bad_secret = " leading-space-secret-value-XYZ"
    with pytest.raises(SettingsError) as info:
        settings_from_mapping(_env(ALPACA_SECRET_KEY=bad_secret, TRADING_MODE="live"))
    message = str(info.value)
    assert "leading-space-secret-value-XYZ" not in message
    assert "ALPACA_SECRET_KEY: invalid" in message
    assert info.value.__cause__ is None  # pydantic error (with inputs) is not chained


def test_secret_values_accessor_is_explicit() -> None:
    s = settings_from_mapping(_env())
    assert s.secret_values() == (FAKE_KEY, FAKE_SECRET)


def test_settings_are_immutable() -> None:
    s = settings_from_mapping(_env())
    with pytest.raises(ValueError, match="frozen"):
        s.trading_mode = "paper"  # type: ignore[misc]


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        Settings.model_validate(
            {"alpaca_api_key": "a", "alpaca_secret_key": "b", "trading_mode": "paper", "x": 1}
        )


def test_load_settings_from_file_with_environment_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"ALPACA_API_KEY={FAKE_KEY}\nALPACA_SECRET_KEY={FAKE_SECRET}\n"
        "TRADING_MODE=paper\nTRADING_ENABLED=true\nUNRELATED=1\n"
    )
    s = load_settings(env_file, environ={"TRADING_ENABLED": "false", "PATH": "/bin"})
    assert s.trading_enabled is False  # process environment wins


def test_load_settings_does_not_mutate_os_environ(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"ALPACA_API_KEY={FAKE_KEY}\nALPACA_SECRET_KEY={FAKE_SECRET}\nTRADING_MODE=paper\n"
    )
    load_settings(env_file, environ={})
    assert "ALPACA_API_KEY" not in os.environ


def test_load_settings_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="env file not found"):
        load_settings(tmp_path / "absent.env", environ={})


def test_env_example_lists_every_setting() -> None:
    """``.env.example`` (no real values) documents every variable the loader reads."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    names = {
        line.split("=", 1)[0].strip()
        for line in example.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert set(ENV_TO_FIELD) <= names
