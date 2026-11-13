"""Step 15B.4: account configuration payload compatibility (Policy B for ``pdt_check`` and
``dtbp_check``).

The paper ``/v2/account/configurations`` payload observed in Step 15B run #2 omits both
fields. They are informational/audit only: absent or ``null`` -> ``None`` (not reported;
never interpreted as a restriction on or off); a present value must be a non-empty string.
``no_shorting``, ``max_margin_multiplier``, ``suspend_trade`` and ``fractional_trading``
stay exactly as strict as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import account_config_from_raw
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from tests.support.alpaca_fake import CONFIG

OPTIONAL = ("pdt_check", "dtbp_check")
REQUIRED = ("no_shorting", "max_margin_multiplier", "suspend_trade", "fractional_trading")
OBSERVED_SHAPE = {
    "closing_transactions_only": bool, "disable_overnight_trading": bool,
    "fractional_trading": bool, "max_margin_multiplier": str, "no_shorting": bool,
    "ptp_no_exception_entry": bool, "suspend_trade": bool, "trade_confirm_email": str,
}  # fmt: skip


def parse(**changes: Any) -> Any:
    return account_config_from_raw({**CONFIG, **changes})


def without(*keys: str) -> dict[str, Any]:
    return {k: v for k, v in CONFIG.items() if k not in keys}


def test_synthetic_config_has_the_observed_names_and_json_types() -> None:
    assert {k: type(v) for k, v in CONFIG.items()} == OBSERVED_SHAPE


# ============================================================ pdt_check / dtbp_check
@pytest.mark.parametrize("key", OPTIONAL)
def test_missing_is_not_reported(key: str) -> None:
    assert getattr(account_config_from_raw(without(key)), key) is None


@pytest.mark.parametrize("key", OPTIONAL)
def test_null_is_not_reported(key: str) -> None:
    assert getattr(parse(**{key: None}), key) is None


@pytest.mark.parametrize("key", OPTIONAL)
@pytest.mark.parametrize("value", ["entry", "exit", "both", "some-future-value"])
def test_present_string_is_kept_verbatim(key: str, value: str) -> None:
    assert getattr(parse(**{key: value}), key) == value


@pytest.mark.parametrize("key", OPTIONAL)
@pytest.mark.parametrize("value", ["", True, False, 0, 1, 1.5, [], ["entry"], {}, {"a": 1}])
def test_malformed_present_value_is_a_payload_violation(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: value})


def test_both_missing_together_parses_and_other_fields_are_unchanged() -> None:
    cfg = account_config_from_raw(without(*OPTIONAL))
    assert (cfg.pdt_check, cfg.dtbp_check) == (None, None)
    known = parse(pdt_check="entry", dtbp_check="entry")
    for name in ("no_shorting", "max_margin_multiplier", "suspend_trade", "fractional_trading"):
        assert getattr(cfg, name) == getattr(known, name), name
    assert cfg.m2_protections_in_place == known.m2_protections_in_place


# ============================================================ everything else stays strict
@pytest.mark.parametrize("key", REQUIRED)
def test_required_config_field_missing_is_rejected(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        account_config_from_raw(without(key))


@pytest.mark.parametrize("key", REQUIRED)
def test_required_config_field_null_is_rejected(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: None})


@pytest.mark.parametrize("key", ["no_shorting", "suspend_trade", "fractional_trading"])
@pytest.mark.parametrize("value", ["false", 0, 1, "true"])
def test_config_booleans_still_require_real_booleans(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: value})


@pytest.mark.parametrize("value", [1.0, "", "one", True])
def test_max_margin_multiplier_still_strict(value: object) -> None:
    with pytest.raises(InvalidBrokerDataError):
        parse(max_margin_multiplier=value)


# ============================================================ simulation parity
AS_OF = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)


def test_sim_defaults_unchanged() -> None:
    cfg = SimBroker(SimClock(AS_OF), SimConfig()).get_account_config()
    assert (cfg.pdt_check, cfg.dtbp_check) == ("entry", "entry")


@pytest.mark.parametrize(("pdt", "dtbp"), [(None, None), (None, "entry"), ("exit", None)])
def test_sim_reports_configured_values(pdt: str | None, dtbp: str | None) -> None:
    sim = SimBroker(SimClock(AS_OF), SimConfig(pdt_check=pdt, dtbp_check=dtbp))
    for _ in range(2):  # deterministic
        cfg = sim.get_account_config()
        assert (cfg.pdt_check, cfg.dtbp_check) == (pdt, dtbp)
