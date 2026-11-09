"""Decimal helpers for money, prices and share quantities.

Rules (ARCHITECTURE §6.1, D9):

* Binary floating point is never accepted. ``float`` inputs raise ``MoneyError``.
* All arithmetic runs in an explicit context with traps for invalid operations,
  division by zero and overflow, so silent NaN/Infinity cannot appear.
* Values are stored and transported as strings; ``to_decimal`` is the single entry
  point for converting external values.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Annotated, Final

from pydantic import AfterValidator, BeforeValidator, PlainSerializer

DECIMAL_CONTEXT: Final = decimal.Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)

_PARSE_CONTEXT: Final = decimal.Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.Inexact, decimal.Overflow, decimal.Underflow],
)
"""Parsing context: a value that cannot be represented exactly raises instead of rounding."""

CASH_QUANTUM: Final = Decimal("0.0001")
DISPLAY_CASH_QUANTUM: Final = Decimal("0.01")
PRICE_QUANTUM: Final = Decimal("0.0001")
PENNY: Final = Decimal("0.01")
SUB_PENNY_THRESHOLD: Final = Decimal("1.00")
SHARE_QUANTUM: Final = Decimal("1")
ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")


class MoneyError(ValueError):
    """Raised for any value that cannot be safely represented as an exact Decimal."""


def to_decimal(value: object) -> Decimal:
    """Convert ``str``, ``int`` or ``Decimal`` to a finite ``Decimal``.

    ``float`` and ``bool`` are rejected outright: a float has already lost precision
    by the time it reaches us, and ``bool`` is an ``int`` subclass that is never a
    meaningful amount.
    """
    if isinstance(value, bool):
        raise MoneyError("bool is not a numeric amount")
    if isinstance(value, float):
        raise MoneyError("float values are not accepted; pass a string or Decimal")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyError("empty string is not a numeric amount")
        try:
            result = Decimal(text)
        except decimal.InvalidOperation as exc:
            raise MoneyError(f"not a decimal number: {text!r}") from exc
    else:
        raise MoneyError(f"unsupported numeric type: {type(value).__name__}")
    if not result.is_finite():
        raise MoneyError("NaN and Infinity are not accepted")
    try:
        result = _PARSE_CONTEXT.plus(result)
    except decimal.Inexact:
        raise MoneyError("value has more than 34 significant digits") from None
    if result.is_zero():
        return ZERO  # normalise -0
    return result


def parse_broker_decimal(value: object, *, field: str) -> Decimal:
    """Parse a numeric field from a raw broker payload.

    Broker JSON carries numbers as strings (ARCHITECTURE §3 V3/V4). Anything else,
    in particular a float produced by an SDK model, is treated as a mapping error.
    """
    if not isinstance(value, str):
        raise MoneyError(f"broker field {field!r} must be a string, got {type(value).__name__}")
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise MoneyError(f"broker field {field!r}: {exc}") from exc


def _quantize(value: Decimal, quantum: Decimal, rounding: str) -> Decimal:
    return value.quantize(quantum, rounding=rounding, context=DECIMAL_CONTEXT)


def quantize_cash(value: Decimal, *, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """Internal cash precision (0.0001)."""
    return _quantize(value, CASH_QUANTUM, rounding)


def reservation_amount(value: Decimal) -> Decimal:
    """Round a cash reservation *up* so a reservation never under-covers a cost."""
    return _quantize(value, CASH_QUANTUM, ROUND_CEILING)


def conservative_proceeds(value: Decimal) -> Decimal:
    """Round expected proceeds *down* so availability is never overstated."""
    return _quantize(value, CASH_QUANTUM, ROUND_FLOOR)


def display_cash(value: Decimal) -> str:
    """Two-decimal string for reports. Never feed the result back into arithmetic."""
    return f"{_quantize(value, DISPLAY_CASH_QUANTUM, ROUND_HALF_EVEN):.2f}"


def is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value(context=DECIMAL_CONTEXT)


def whole_shares(value: object) -> Decimal:
    """Validate a strictly positive whole-share quantity (D3)."""
    qty = to_decimal(value)
    if qty <= ZERO:
        raise MoneyError("share quantity must be positive")
    if not is_whole(qty):
        raise MoneyError("fractional shares are not permitted")
    return _quantize(qty, SHARE_QUANTUM, ROUND_HALF_EVEN)


def valid_order_price(value: object) -> Decimal:
    """Validate a limit/stop price for US equities.

    Prices at or above $1.00 must be whole cents (sub-penny rule); prices below
    $1.00 may use up to four decimals.
    """
    price = to_decimal(value)
    if price <= ZERO:
        raise MoneyError("price must be positive")
    quantum = PENNY if price >= SUB_PENNY_THRESHOLD else PRICE_QUANTUM
    if price != _quantize(price, quantum, ROUND_HALF_EVEN):
        raise MoneyError(f"price {price} exceeds allowed precision {quantum}")
    return price


def _positive(value: Decimal) -> Decimal:
    if value <= ZERO:
        raise MoneyError("value must be positive")
    return value


def _non_negative(value: Decimal) -> Decimal:
    if value < ZERO:
        raise MoneyError("value must not be negative")
    return value


def _fraction(value: Decimal) -> Decimal:
    if not ZERO < value <= ONE:
        raise MoneyError("fraction must be in (0, 1]")
    return value


_as_str = PlainSerializer(str, return_type=str)

DecimalValue = Annotated[Decimal, BeforeValidator(to_decimal), _as_str]
PositiveDecimal = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(_positive), _as_str
]
NonNegativeDecimal = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(_non_negative), _as_str
]
Fraction = Annotated[Decimal, BeforeValidator(to_decimal), AfterValidator(_fraction), _as_str]
Shares = Annotated[Decimal, BeforeValidator(whole_shares), _as_str]
OrderPrice = Annotated[Decimal, BeforeValidator(valid_order_price), _as_str]
