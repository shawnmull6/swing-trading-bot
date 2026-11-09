"""Value encoding between domain types and SQLite TEXT columns.

* Decimals are written as canonical positional text (``format(d, "f")``): no
  exponent, no NaN/Infinity. Only ``Decimal`` is accepted on write, so a float can
  never reach the database (STRICT tables would otherwise coerce REAL to TEXT).
* Timestamps must be timezone-aware; they are stored in UTC with fixed microsecond
  precision so lexical order equals chronological order.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from swing_trading_bot.domain.money import MoneyError, to_decimal

_TS_FORMAT_LEN = len("2026-01-01T00:00:00.000000+00:00")


class CodecError(ValueError):
    pass


def dec_to_db(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise CodecError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise CodecError("non-finite Decimal cannot be stored")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if text.endswith("."):  # pragma: no cover - format('f') never emits this
        text = text[:-1]
    return text


def opt_dec_to_db(value: Decimal | None) -> str | None:
    return None if value is None else dec_to_db(value)


def dec_from_db(value: object) -> Decimal:
    if not isinstance(value, str):
        raise CodecError(f"stored decimal must be TEXT, got {type(value).__name__}")
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise CodecError(f"invalid stored decimal: {exc}") from None


def opt_dec_from_db(value: object) -> Decimal | None:
    return None if value is None else dec_from_db(value)


def ts_to_db(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CodecError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def opt_ts_to_db(value: datetime | None) -> str | None:
    return None if value is None else ts_to_db(value)


def ts_from_db(value: object) -> datetime:
    if not isinstance(value, str) or len(value) != _TS_FORMAT_LEN:
        raise CodecError("invalid stored timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise CodecError("stored timestamp is not UTC")
    return parsed.astimezone(UTC)


def opt_ts_from_db(value: object) -> datetime | None:
    return None if value is None else ts_from_db(value)


def date_to_db(value: date) -> str:
    if isinstance(value, datetime):
        raise CodecError("expected a date, got a datetime")
    return value.isoformat()


def opt_date_to_db(value: date | None) -> str | None:
    return None if value is None else date_to_db(value)


def date_from_db(value: object) -> date:
    if not isinstance(value, str):
        raise CodecError("invalid stored date")
    return date.fromisoformat(value)


def opt_date_from_db(value: object) -> date | None:
    return None if value is None else date_from_db(value)


def str_from_db(value: object) -> str:
    if not isinstance(value, str):
        raise CodecError(f"expected TEXT, got {type(value).__name__}")
    return value


def opt_str_from_db(value: object) -> str | None:
    return None if value is None else str_from_db(value)


def int_from_db(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodecError(f"expected INTEGER, got {type(value).__name__}")
    return value


def opt_int_from_db(value: object) -> int | None:
    return None if value is None else int_from_db(value)
