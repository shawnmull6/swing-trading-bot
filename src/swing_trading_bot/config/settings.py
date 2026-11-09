"""Process settings loaded from the environment / ``.env`` (ARCHITECTURE §13).

Safety properties:

* ``TRADING_MODE`` must be ``paper``; anything else is a startup failure.
* ``TRADING_ENABLED`` defaults to ``False`` and only accepts unambiguous values.
* Credentials are held as ``SecretStr``. Validation errors never include input values
  (``hide_input_in_errors``), and :class:`SettingsError` messages list field names and
  error kinds only.
* Loading never mutates ``os.environ`` and never prints anything.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from swing_trading_bot.domain.money import PENNY, PositiveDecimal

ENV_TO_FIELD: Final[Mapping[str, str]] = {
    "ALPACA_API_KEY": "alpaca_api_key",
    "ALPACA_SECRET_KEY": "alpaca_secret_key",
    "TRADING_MODE": "trading_mode",
    "TRADING_ENABLED": "trading_enabled",
    "INITIAL_CAPITAL": "initial_capital",
    "MAX_CAPITAL_ALLOCATION": "max_capital_allocation",
}
FIELD_TO_ENV: Final[Mapping[str, str]] = {v: k for k, v in ENV_TO_FIELD.items()}

_TRUE: Final = frozenset({"true", "1"})
_FALSE: Final = frozenset({"false", "0"})


class SettingsError(ValueError):
    """Configuration is invalid. The message never contains configured values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    alpaca_api_key: SecretStr = Field(repr=False)
    alpaca_secret_key: SecretStr = Field(repr=False)
    trading_mode: Literal["paper"]
    trading_enabled: bool = False
    initial_capital: PositiveDecimal = Decimal("5000")
    max_capital_allocation: PositiveDecimal = Decimal("5000")

    @field_validator("alpaca_api_key", "alpaca_secret_key")
    @classmethod
    def _non_blank_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or raw != raw.strip():
            raise ValueError("must be non-blank without surrounding whitespace")
        return value

    @field_validator("trading_enabled", mode="before")
    @classmethod
    def _strict_bool(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
        raise ValueError("must be one of: true, false, 1, 0")

    @field_validator("initial_capital", "max_capital_allocation")
    @classmethod
    def _whole_cents(cls, value: Decimal) -> Decimal:
        if value != value.quantize(PENNY):
            raise ValueError("must be a whole number of cents")
        return value

    def secret_values(self) -> tuple[str, ...]:
        """Raw secret values, for the log redactor only. Never log or persist these."""
        return (
            self.alpaca_api_key.get_secret_value(),
            self.alpaca_secret_key.get_secret_value(),
        )

    def describe(self) -> dict[str, str]:
        """Non-secret summary suitable for logs and ``status`` output."""
        return {
            "trading_mode": self.trading_mode,
            "trading_enabled": str(self.trading_enabled).lower(),
            "initial_capital": str(self.initial_capital),
            "max_capital_allocation": str(self.max_capital_allocation),
            "alpaca_api_key": "set",
            "alpaca_secret_key": "set",
        }


def _format_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = err["loc"][0] if err["loc"] else "settings"
        name = FIELD_TO_ENV.get(str(loc), str(loc))
        if err["type"] == "missing":
            parts.append(f"{name}: required but not set")
        elif name in {"ALPACA_API_KEY", "ALPACA_SECRET_KEY"}:
            parts.append(f"{name}: invalid ({err['type']})")
        else:
            parts.append(f"{name}: {err['msg']}")
    return "invalid settings: " + "; ".join(sorted(parts))


def settings_from_mapping(env: Mapping[str, str | None]) -> Settings:
    """Build settings from an explicit mapping of environment variable names.

    Unrelated keys are ignored; keys present with ``None``/empty values are treated as
    unset for optional fields and as missing for required ones.
    """
    data: dict[str, str] = {}
    for env_name, field in ENV_TO_FIELD.items():
        value = env.get(env_name)
        if value is None or value.strip() == "":
            continue
        data[field] = value
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(_format_errors(exc)) from None


def load_settings(
    env_file: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from ``env_file`` overlaid by the process environment.

    Process environment variables take precedence over the file. ``env_file=None``
    uses the environment only. The file is read with ``dotenv_values``, which does not
    modify ``os.environ``.
    """
    merged: dict[str, str | None] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise SettingsError(f"env file not found: {env_file}")
        merged.update(dotenv_values(env_file))
    source = os.environ if environ is None else environ
    merged.update({k: v for k, v in source.items() if k in ENV_TO_FIELD})
    return settings_from_mapping(merged)
