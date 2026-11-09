"""Step 4: value codec between domain types and SQLite TEXT."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.ledger.codec import (
    CodecError,
    date_from_db,
    date_to_db,
    dec_from_db,
    dec_to_db,
    int_from_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("5000"), "5000"),
        (Decimal("5000.00"), "5000.00"),
        (Decimal("-0.0001"), "-0.0001"),
        (Decimal("1E+3"), "1000"),
        (Decimal("1.5E-7"), "0.00000015"),
        (Decimal("-0"), "0"),
        (Decimal("0E-10"), "0"),
    ],
)
def test_decimal_canonical_text(value: Decimal, text: str) -> None:
    assert dec_to_db(value) == text
    assert dec_from_db(text) == value


@pytest.mark.parametrize("value", [1.5, 1, "1.5", None, True])
def test_only_decimal_can_be_written(value: object) -> None:
    with pytest.raises(CodecError, match="expected Decimal"):
        dec_to_db(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_rejected(value: Decimal) -> None:
    with pytest.raises(CodecError, match="non-finite"):
        dec_to_db(value)


@pytest.mark.parametrize("value", [1.5, 3, None, b"1", "abc", "NaN"])
def test_invalid_stored_decimal(value: object) -> None:
    with pytest.raises(CodecError):
        dec_from_db(value)


@given(
    st.decimals(
        allow_nan=False,
        allow_infinity=False,
        min_value=Decimal("-1e15"),
        max_value=Decimal("1e15"),
        places=12,
    )
)
def test_decimal_round_trip_is_exact(value: Decimal) -> None:
    text = dec_to_db(value)
    assert "E" not in text and "e" not in text
    assert dec_from_db(text) == value


def test_over_precise_stored_value_rejected() -> None:
    with pytest.raises(CodecError, match="34 significant digits"):
        dec_from_db("0." + "1" * 35)


def test_timestamps_are_utc_fixed_width() -> None:
    eastern = timezone(timedelta(hours=-5))
    local = datetime(2026, 11, 9, 10, 30, tzinfo=eastern)
    text = ts_to_db(local)
    assert text == "2026-11-09T15:30:00.000000+00:00"
    assert ts_from_db(text) == local
    assert ts_from_db(text).tzinfo is UTC


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(CodecError, match="timezone-aware"):
        ts_to_db(datetime(2026, 11, 9))  # noqa: DTZ001


@pytest.mark.parametrize(
    "text", ["2026-11-09T15:30:00+00:00", "2026-11-09T10:30:00.000000-05:00", "x", 5]
)
def test_invalid_stored_timestamps(text: object) -> None:
    with pytest.raises(CodecError):
        ts_from_db(text)


@given(st.datetimes(timezones=st.just(UTC)))
def test_timestamp_round_trip_and_ordering(value: datetime) -> None:
    assert ts_from_db(ts_to_db(value)) == value
    later = value + timedelta(microseconds=1) if value.year < 9999 else value
    assert (ts_to_db(value) <= ts_to_db(later)) is True


def test_dates() -> None:
    assert date_to_db(date(2026, 11, 9)) == "2026-11-09"
    assert date_from_db("2026-11-09") == date(2026, 11, 9)
    with pytest.raises(CodecError):
        date_to_db(datetime(2026, 11, 9, tzinfo=UTC))
    with pytest.raises(CodecError):
        date_from_db(20261109)


def test_scalar_type_guards() -> None:
    assert int_from_db(3) == 3
    assert str_from_db("a") == "a"
    with pytest.raises(CodecError):
        int_from_db(True)
    with pytest.raises(CodecError):
        int_from_db("3")
    with pytest.raises(CodecError):
        str_from_db(3)
