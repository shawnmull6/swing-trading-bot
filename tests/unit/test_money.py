"""Step 3: Decimal helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.money import (
    MoneyError,
    conservative_proceeds,
    display_cash,
    parse_broker_decimal,
    quantize_cash,
    reservation_amount,
    to_decimal,
    valid_order_price,
    whole_shares,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5000", Decimal("5000")),
        (" 12.34 ", Decimal("12.34")),
        (7, Decimal(7)),
        (Decimal("0.0001"), Decimal("0.0001")),
        ("-0", Decimal("0")),
        ("1E+3", Decimal("1000")),
    ],
)
def test_to_decimal_accepts_exact_inputs(value: object, expected: Decimal) -> None:
    assert to_decimal(value) == expected


@pytest.mark.parametrize(
    "value", [0.1, 1.0, True, False, None, "", "  ", "abc", "NaN", "inf", "-Infinity", [1]]
)
def test_to_decimal_rejects_unsafe_inputs(value: object) -> None:
    with pytest.raises(MoneyError):
        to_decimal(value)


def test_to_decimal_rejects_non_finite_decimal() -> None:
    with pytest.raises(MoneyError):
        to_decimal(Decimal("NaN"))


@pytest.mark.parametrize(
    "value",
    ["2.8544474612446828260702551350541759E-18", "1234567890123456789012345678901234.5"],
)
def test_over_precise_values_rejected_not_rounded(value: str) -> None:
    with pytest.raises(MoneyError, match="34 significant digits"):
        to_decimal(value)
    with pytest.raises(MoneyError, match="34 significant digits"):
        to_decimal(Decimal(value))


def test_trailing_zeros_beyond_precision_are_exact() -> None:
    assert to_decimal("1." + "0" * 40) == Decimal(1)


def test_negative_zero_normalised() -> None:
    assert str(to_decimal(Decimal("-0.00"))) == "0"


def test_parse_broker_decimal_requires_strings() -> None:
    assert parse_broker_decimal("101.25", field="price") == Decimal("101.25")
    with pytest.raises(MoneyError, match="'price' must be a string"):
        parse_broker_decimal(101.25, field="price")
    with pytest.raises(MoneyError, match="'qty' must be a string"):
        parse_broker_decimal(10, field="qty")
    with pytest.raises(MoneyError, match="'qty'"):
        parse_broker_decimal("ten", field="qty")


def test_rounding_directions() -> None:
    assert quantize_cash(Decimal("1.00005")) == Decimal("1.0000")  # half-even
    assert quantize_cash(Decimal("1.00015")) == Decimal("1.0002")
    assert reservation_amount(Decimal("1.00001")) == Decimal("1.0001")  # up
    assert conservative_proceeds(Decimal("1.00019")) == Decimal("1.0001")  # down
    assert display_cash(Decimal("4999.995")) == "5000.00"
    assert display_cash(Decimal("5000")) == "5000.00"


@pytest.mark.parametrize("value", ["1", "10", 25, Decimal("100"), "3.000"])
def test_whole_shares_valid(value: object) -> None:
    assert whole_shares(value) == Decimal(str(value)).quantize(Decimal(1))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "0.0001", 1.0])
def test_whole_shares_invalid(value: object) -> None:
    with pytest.raises(MoneyError):
        whole_shares(value)


@pytest.mark.parametrize("value", ["10.25", "1.00", "0.9999", "0.5", "250"])
def test_valid_order_price(value: str) -> None:
    assert valid_order_price(value) == Decimal(value)


@pytest.mark.parametrize("value", ["10.251", "1.005", "0.00001", "0", "-5"])
def test_invalid_order_price(value: str) -> None:
    with pytest.raises(MoneyError):
        valid_order_price(value)


cents = st.decimals(min_value=Decimal("-1e9"), max_value=Decimal("1e9"), places=4)


@given(cents)
def test_string_round_trip_is_exact(value: Decimal) -> None:
    assert to_decimal(str(value)) == value


@given(st.decimals(min_value=Decimal(0), max_value=Decimal("1e9"), places=8))
def test_reservation_never_under_covers(value: Decimal) -> None:
    assert reservation_amount(value) >= value
    assert reservation_amount(value) - value < Decimal("0.0001")


@given(st.decimals(min_value=Decimal(0), max_value=Decimal("1e9"), places=8))
def test_proceeds_never_overstated(value: Decimal) -> None:
    assert conservative_proceeds(value) <= value


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_every_float_is_rejected(value: float) -> None:
    with pytest.raises(MoneyError):
        to_decimal(value)
