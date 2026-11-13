"""Step 15B.2: account payload compatibility (Policy B for ``pattern_day_trader``).

The Alpaca paper ``/v2/account`` payload observed in Step 15B omits
``pattern_day_trader``. It is informational only: absent or ``null`` maps to ``None``
(unknown, never ``False``); a present non-boolean is still a broker-payload violation; and
every other account field stays exactly as strict as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import account_from_raw, opt_boolean
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from tests.support.alpaca_fake import ACCOUNT

AS_OF = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
#: Every account field the mapper requires (all but ``pattern_day_trader``).
REQUIRED = (
    "id", "status", "currency", "cash", "non_marginable_buying_power", "multiplier",
    "trading_blocked", "account_blocked", "accrued_fees", "pending_reg_taf_fees",
)  # fmt: skip


def parse(**changes: Any) -> Any:
    raw = {**ACCOUNT, **changes}
    return account_from_raw(raw, as_of=AS_OF, environment="paper")


def without(*keys: str) -> dict[str, Any]:
    return {k: v for k, v in ACCOUNT.items() if k not in keys}


# ============================================================ PDT matrix
def test_default_synthetic_account_has_no_pdt_field() -> None:
    assert "pattern_day_trader" not in ACCOUNT and "daytrade_count" not in ACCOUNT


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
def test_pdt_present_booleans_are_kept(value: bool, expected: bool) -> None:
    assert parse(pattern_day_trader=value).pattern_day_trader is expected


def test_pdt_missing_is_unknown_not_false() -> None:
    account = account_from_raw(without("pattern_day_trader"), as_of=AS_OF, environment="paper")
    assert account.pattern_day_trader is None
    assert account.pattern_day_trader is not False


def test_pdt_null_is_unknown_not_false() -> None:
    assert parse(pattern_day_trader=None).pattern_day_trader is None


@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, "0", [], {}, "null"])
def test_pdt_malformed_is_a_payload_violation(value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match="pattern_day_trader"):
        parse(pattern_day_trader=value)


def test_opt_boolean_helper() -> None:
    assert opt_boolean({}, "k") is None
    assert opt_boolean({"k": None}, "k") is None
    assert opt_boolean({"k": False}, "k") is False
    assert opt_boolean({"k": True}, "k") is True
    with pytest.raises(InvalidBrokerDataError):
        opt_boolean({"k": "False"}, "k")


# ============================================================ every other field stays strict
@pytest.mark.parametrize("key", REQUIRED)
def test_required_field_missing_is_rejected(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        account_from_raw(without(key), as_of=AS_OF, environment="paper")


@pytest.mark.parametrize("key", REQUIRED)
def test_required_field_null_is_rejected(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: None})


@pytest.mark.parametrize("key", ["trading_blocked", "account_blocked"])
@pytest.mark.parametrize("value", ["false", 0, 1])
def test_restriction_flags_still_require_real_booleans(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: value})


@pytest.mark.parametrize("key", ["cash", "non_marginable_buying_power", "multiplier",
                                 "accrued_fees", "pending_reg_taf_fees"])  # fmt: skip
def test_money_fields_still_reject_floats(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError):
        parse(**{key: 1.5})


def test_unknown_pdt_does_not_change_any_other_field() -> None:
    unknown = account_from_raw(without("pattern_day_trader"), as_of=AS_OF, environment="paper")
    known = parse(pattern_day_trader=False)
    for name in ("account_id", "status", "currency", "cash", "non_marginable_buying_power",
                 "multiplier", "trading_blocked", "account_blocked", "accrued_fees",
                 "pending_reg_taf_fees", "environment", "as_of"):  # fmt: skip
        assert getattr(unknown, name) == getattr(known, name), name


# ============================================================ simulation stays deterministic
def test_sim_default_still_reports_false() -> None:
    assert SimBroker(SimClock(AS_OF), SimConfig()).get_account().pattern_day_trader is False


@pytest.mark.parametrize("value", [True, False, None])
def test_sim_reports_configured_pdt(value: bool | None) -> None:
    sim = SimBroker(SimClock(AS_OF), SimConfig(pattern_day_trader=value))
    first, second = sim.get_account(), sim.get_account()
    assert first.pattern_day_trader is value and second.pattern_day_trader is value
