"""Step 3: broker status mapping is exhaustive and fails closed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from alpaca.trading.enums import OrderStatus as SdkOrderStatus

from swing_trading_bot.domain.broker_status import (
    BROKER_STATUS_RULES,
    Disposition,
    InconsistentBrokerOrderError,
    UnexpectedBrokerStatusError,
    UnmappedBrokerStatusError,
    local_status_for,
    rule_for,
)
from swing_trading_bot.domain.enums import OrderStatus as S

Q = Decimal(10)
Z = Decimal(0)


def test_every_sdk_status_is_mapped() -> None:
    """A new alpaca-py status value must fail this test, not production."""
    sdk_values = {member.value for member in SdkOrderStatus.__members__.values()}
    assert sdk_values == set(BROKER_STATUS_RULES)


def test_terminal_rules_name_a_terminal_status() -> None:
    for status, rule in BROKER_STATUS_RULES.items():
        assert (rule.disposition is Disposition.TERMINAL) == (rule.terminal_status is not None), (
            status
        )


@pytest.mark.parametrize("status", ["", "NEW", "filled ", "unknown_future_status"])
def test_unmapped_status_raises(status: str) -> None:
    with pytest.raises(UnmappedBrokerStatusError):
        rule_for(status)


@pytest.mark.parametrize("status", ["replaced", "pending_replace"])
def test_replace_statuses_are_unexpected_in_m1(status: str) -> None:
    with pytest.raises(UnexpectedBrokerStatusError):
        local_status_for(status, filled_qty=Z, qty=Q)


@pytest.mark.parametrize(
    ("status", "filled", "expected"),
    [
        ("new", Z, S.ACCEPTED),
        ("pending_new", Z, S.ACCEPTED),
        ("accepted", Z, S.ACCEPTED),
        ("held", Z, S.ACCEPTED),
        ("new", Decimal(3), S.PARTIALLY_FILLED),
        ("partially_filled", Decimal(3), S.PARTIALLY_FILLED),
        ("done_for_day", Decimal(3), S.PARTIALLY_FILLED),
        ("calculated", Q, S.PARTIALLY_FILLED),  # full qty but not terminal: never inferred
        ("pending_cancel", Decimal(3), S.PENDING_CANCEL),
        ("filled", Q, S.FILLED),
        ("canceled", Z, S.CANCELED),
        ("canceled", Decimal(4), S.CANCELED),
        ("expired", Decimal(4), S.EXPIRED),
        ("rejected", Z, S.REJECTED),
    ],
)
def test_mapping(status: str, filled: Decimal, expected: S) -> None:
    assert local_status_for(status, filled_qty=filled, qty=Q) is expected


@pytest.mark.parametrize(
    ("status", "filled"),
    [
        ("filled", Decimal(9)),
        ("rejected", Decimal(1)),
        ("partially_filled", Z),
        ("new", Decimal(11)),
        ("new", Decimal(-1)),
    ],
)
def test_inconsistent_combinations_raise(status: str, filled: Decimal) -> None:
    with pytest.raises(InconsistentBrokerOrderError):
        local_status_for(status, filled_qty=filled, qty=Q)


@pytest.mark.parametrize("status", ["stopped", "suspended", "pending_review"])
def test_attention_statuses_flagged(status: str) -> None:
    assert rule_for(status).attention
