"""Strict mapping from raw broker JSON (Alpaca v2 shape) to typed snapshots.

Broker JSON carries numbers as strings (ARCHITECTURE §3 V3/V4). Every numeric field is
parsed with :func:`parse_broker_decimal`, which rejects floats; every timestamp must
carry a UTC offset; every enum value must be known. Any deviation raises
:class:`InvalidBrokerDataError` — nothing is defaulted, coerced or guessed.

This module does not import a broker SDK. :class:`~swing_trading_bot.broker.sim.SimBroker`
routes all its responses through it, and the step 9 Alpaca read adapter will reuse it.
The broker's ``buying_power`` field is deliberately never read.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from swing_trading_bot.broker.errors import InvalidBrokerDataError, UnknownBrokerStatusError
from swing_trading_bot.broker.models import (
    AccountConfigSnapshot,
    AccountSnapshot,
    BrokerActivity,
    MarketClock,
)
from swing_trading_bot.domain.broker_status import BROKER_STATUS_RULES
from swing_trading_bot.domain.calendar import MARKET_TZ, MAX_SETTLEMENT_LAG_DAYS, TradingSession
from swing_trading_bot.domain.enums import OrderType, Side, TimeInForce
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition
from swing_trading_bot.domain.money import MoneyError, parse_broker_decimal

Raw = Mapping[str, Any]

_SIDES: Final = {"buy": Side.BUY, "sell": Side.SELL}
_ORDER_TYPES: Final = {
    "limit": OrderType.LIMIT,
    "market": OrderType.MARKET,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
}
_TIFS: Final = {"day": TimeInForce.DAY, "gtc": TimeInForce.GTC}

MAX_PAYLOAD_DEPTH: Final = 64
"""Deepest container nesting accepted in broker JSON (Step 15B.6B). Convention: the
outermost array/object is depth 1, each nested array/object adds 1, scalars add nothing
(``[]`` is 1, ``{"a": [1]}`` is 2, a bare scalar is 0). Depth 64 is accepted, 65 is an
:class:`InvalidBrokerDataError`. Alpaca payloads are a few levels deep; the limit keeps
every recursive step (the JSON decoder, :func:`raw_json`) far from Python's recursion
limit. It equals the contract evidence limit, so an accepted payload is also storable."""


def check_payload_depth(value: object) -> None:
    """Reject nesting deeper than :data:`MAX_PAYLOAD_DEPTH` (iterative; never recurses)."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            children: Iterable[object] = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > MAX_PAYLOAD_DEPTH:
            raise InvalidBrokerDataError(f"payload nesting exceeds {MAX_PAYLOAD_DEPTH} levels")
        stack.extend((child, depth + 1) for child in children)


def raw_json(raw: Raw) -> str:
    """Canonical, deterministic JSON text of a raw broker payload (Step 15B.5B.1).

    Byte-identical to ``json.dumps(raw, sort_keys=True, separators=(",", ":"))`` for
    every value that call accepts and that is valid JSON. In addition:

    * a finite :class:`~decimal.Decimal` — what ``decode_json(parse_float=Decimal)``
      yields for a JSON number with a fraction or exponent — is written as that exact
      JSON number (``str(value)``, e.g. ``1.50``, ``1E+5``), never via ``float`` and
      never as a string. Whether a *mapped* field may be a number is decided by its own
      validator (money fields require strings), not here;
    * a value that is not valid JSON — NaN or ±Infinity, as ``float`` (the decoder's
      ``parse_constant`` default) or ``Decimal`` — is a malformed broker payload:
      :class:`InvalidBrokerDataError`.

    Anything the JSON decoder cannot produce (non-string keys, sets, bytes, arbitrary
    objects) still raises ``TypeError``: that is a programming defect, not a broker
    payload, and is deliberately not reclassified. Nesting deeper than
    :data:`MAX_PAYLOAD_DEPTH` is a malformed payload (Step 15B.6B):
    :class:`InvalidBrokerDataError`, raised before any recursion limit is approached.
    """
    parts: list[str] = []
    _encode(raw, parts, 0)
    return "".join(parts)


def _encode(value: object, out: list[str], depth: int) -> None:
    if isinstance(value, (Mapping, list, tuple)) and depth >= MAX_PAYLOAD_DEPTH:
        raise InvalidBrokerDataError(f"payload nesting exceeds {MAX_PAYLOAD_DEPTH} levels")
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("raw broker payload keys must be strings")
        out.append("{")
        for n, key in enumerate(sorted(value)):
            if n:
                out.append(",")
            out.append(json.dumps(key))
            out.append(":")
            _encode(value[key], out, depth + 1)
        out.append("}")
        return
    if isinstance(value, (list, tuple)):
        out.append("[")
        for n, item in enumerate(value):
            if n:
                out.append(",")
            _encode(item, out, depth + 1)
        out.append("]")
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidBrokerDataError("raw payload contains a non-finite number")
        out.append(str(value))
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidBrokerDataError("raw payload contains a non-finite number")
    if value is None or isinstance(value, (str, int, float)):  # bool is an int
        out.append(json.dumps(value))
        return
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _require(raw: Raw, key: str) -> object:
    if key not in raw or raw[key] is None:
        raise InvalidBrokerDataError(f"missing required field {key!r}")
    return raw[key]


def text(raw: Raw, key: str) -> str:
    value = _require(raw, key)
    if not isinstance(value, str) or not value:
        raise InvalidBrokerDataError(f"field {key!r} must be a non-empty string")
    return value


def opt_text(raw: Raw, key: str) -> str | None:
    return None if raw.get(key) is None else text(raw, key)


def decimal(raw: Raw, key: str) -> Decimal:
    try:
        return parse_broker_decimal(_require(raw, key), field=key)
    except MoneyError as exc:
        raise InvalidBrokerDataError(str(exc)) from None


def _activity_description(raw: Raw) -> str | None:
    """Field-specific rule for an activity's free-text ``description`` (Step 15B.5B).

    Absent or ``null`` -> ``None``; any string, **including the empty string**, is kept
    exactly (Alpaca sent ``""`` in Step 15B live run #3); anything else ->
    :class:`InvalidBrokerDataError`. ``""`` and ``None`` stay distinct: FEE twin
    detection compares the stored raw value with this one. :func:`text` and
    :func:`opt_text` are unchanged for every other field.
    """
    value = raw.get("description")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidBrokerDataError("field 'description' must be a string")
    return value


def opt_decimal(raw: Raw, key: str) -> Decimal | None:
    return None if raw.get(key) is None else decimal(raw, key)


def json_number(raw: Raw, key: str) -> Decimal:
    """Exact value of a JSON number decoded with ``parse_float=Decimal`` (market data).
    Accepts Decimal, int or a decimal string; rejects float and bool."""
    value = _require(raw, key)
    if isinstance(value, (bool, float)):
        raise InvalidBrokerDataError(f"field {key!r} is not an exact number")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidBrokerDataError(f"field {key!r} is not finite")
        return value
    if isinstance(value, int):
        return Decimal(value)
    return decimal(raw, key)


def boolean(raw: Raw, key: str) -> bool:
    value = _require(raw, key)
    if not isinstance(value, bool):
        raise InvalidBrokerDataError(f"field {key!r} must be a boolean")
    return value


def opt_boolean(raw: Raw, key: str) -> bool | None:
    """``None`` when the field is absent or ``null`` (unknown, never ``False``); a present
    value must be a JSON boolean."""
    return None if raw.get(key) is None else boolean(raw, key)


def timestamp(raw: Raw, key: str) -> datetime:
    value = text(raw, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidBrokerDataError(f"field {key!r} is not an ISO-8601 timestamp") from None
    if parsed.utcoffset() is None:
        raise InvalidBrokerDataError(f"field {key!r} has no UTC offset")
    return parsed


def settlement_days_from_raw(rows: Sequence[Raw]) -> tuple[date, ...]:
    """Settlement days from Alpaca ``GET /v2/calendar?date_type=SETTLEMENT`` rows.

    Observed shape (Step 15B live run #3, public market calendar): one row per **trade
    date** — ``date`` is the trading session, ``settlement_date`` is when that session's
    trades settle — and the rows are selected by ``settlement_date`` falling inside the
    requested range (so the earliest ``date`` precedes ``start``). The settlement days are
    therefore the distinct ``settlement_date`` values; the trade ``date`` is never used as
    a settlement day.

    Every row needs a valid ``date`` and ``settlement_date`` with
    ``date < settlement_date <= date + MAX_SETTLEMENT_LAG_DAYS``; anything else raises
    :class:`InvalidBrokerDataError`. Several trade dates may share one settlement date
    (e.g. around a bank holiday), so the result is de-duplicated and sorted. Range,
    weekend and conflict checks happen when the fetch is recorded (``CalendarFetch`` /
    ``CalendarStore``).
    """
    days: set[date] = set()
    for raw in rows:
        trade = calendar_date(raw, "date")
        settles = calendar_date(raw, "settlement_date")
        if settles <= trade:
            raise InvalidBrokerDataError(
                f"settlement calendar row {trade}: settlement_date {settles} is not after "
                "the trade date"
            )
        if (settles - trade).days > MAX_SETTLEMENT_LAG_DAYS:
            raise InvalidBrokerDataError(
                f"settlement calendar row {trade}: settlement_date {settles} exceeds the "
                f"{MAX_SETTLEMENT_LAG_DAYS}-day settlement lag"
            )
        days.add(settles)
    return tuple(sorted(days))


def opt_timestamp(raw: Raw, key: str) -> datetime | None:
    return None if raw.get(key) is None else timestamp(raw, key)


def calendar_date(raw: Raw, key: str) -> date:
    value = text(raw, key)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidBrokerDataError(f"field {key!r} is not a YYYY-MM-DD date") from None


def _enum[T](raw: Raw, key: str, table: Mapping[str, T]) -> T:
    value = text(raw, key)
    try:
        return table[value]
    except KeyError:
        raise InvalidBrokerDataError(f"field {key!r} has unknown value {value!r}") from None


def order_status(raw: Raw) -> str:
    status = text(raw, "status")
    if status not in BROKER_STATUS_RULES:
        raise UnknownBrokerStatusError(f"unknown order status {status!r}")
    return status


def order_from_raw(raw: Raw) -> BrokerOrderSnapshot:
    status = order_status(raw)
    try:
        return BrokerOrderSnapshot(
            client_order_id=text(raw, "client_order_id"),
            broker_order_id=text(raw, "id"),
            symbol=text(raw, "symbol"),
            side=_enum(raw, "side", _SIDES),
            order_type=_enum(raw, "type", _ORDER_TYPES),
            time_in_force=_enum(raw, "time_in_force", _TIFS),
            qty=decimal(raw, "qty"),
            filled_qty=decimal(raw, "filled_qty"),
            filled_avg_price=opt_decimal(raw, "filled_avg_price"),
            limit_price=opt_decimal(raw, "limit_price"),
            broker_status=status,
            submitted_at=opt_timestamp(raw, "submitted_at"),
            updated_at=timestamp(raw, "updated_at"),
        )
    except ValidationError as exc:
        raise InvalidBrokerDataError(
            f"order payload invalid: {exc.error_count()} error(s)"
        ) from None


def position_from_raw(raw: Raw) -> BrokerPosition:
    try:
        return BrokerPosition(
            symbol=text(raw, "symbol"),
            qty=decimal(raw, "qty"),
            avg_entry_price=decimal(raw, "avg_entry_price"),
        )
    except ValidationError as exc:
        raise InvalidBrokerDataError(
            f"position payload invalid: {exc.error_count()} error(s)"
        ) from None


def account_from_raw(raw: Raw, *, as_of: datetime, environment: str) -> AccountSnapshot:
    return AccountSnapshot(
        as_of=as_of,
        account_id=text(raw, "id"),
        environment=environment,
        status=text(raw, "status"),
        currency=text(raw, "currency"),
        cash=decimal(raw, "cash"),
        non_marginable_buying_power=decimal(raw, "non_marginable_buying_power"),
        multiplier=decimal(raw, "multiplier"),
        # Informational; absent/null -> None (unknown). Every other field stays required.
        pattern_day_trader=opt_boolean(raw, "pattern_day_trader"),
        trading_blocked=boolean(raw, "trading_blocked"),
        account_blocked=boolean(raw, "account_blocked"),
        accrued_fees=decimal(raw, "accrued_fees"),
        pending_reg_taf_fees=decimal(raw, "pending_reg_taf_fees"),
    )


def account_config_from_raw(raw: Raw) -> AccountConfigSnapshot:
    return AccountConfigSnapshot(
        no_shorting=boolean(raw, "no_shorting"),
        max_margin_multiplier=decimal(raw, "max_margin_multiplier"),
        # Informational/audit only: absent/null -> None; a present value must be a
        # non-empty string. Every other configuration field stays required.
        pdt_check=opt_text(raw, "pdt_check"),
        dtbp_check=opt_text(raw, "dtbp_check"),
        suspend_trade=boolean(raw, "suspend_trade"),
        fractional_trading=boolean(raw, "fractional_trading"),
    )


def clock_from_raw(raw: Raw) -> MarketClock:
    return MarketClock(
        timestamp=timestamp(raw, "timestamp"),
        is_open=boolean(raw, "is_open"),
        next_open=timestamp(raw, "next_open"),
        next_close=timestamp(raw, "next_close"),
    )


def activity_from_raw(raw: Raw) -> BrokerActivity:
    activity_id = text(raw, "id")
    activity_type = text(raw, "activity_type")
    if activity_type == "FILL":
        occurred = timestamp(raw, "transaction_time")
        qty = decimal(raw, "qty")
        price = decimal(raw, "price")
        if qty <= 0 or price <= 0:
            raise InvalidBrokerDataError(f"fill {activity_id}: qty and price must be positive")
        return BrokerActivity(
            activity_id=activity_id,
            activity_type=activity_type,
            activity_date=occurred.astimezone(MARKET_TZ).date(),
            occurred_at=occurred,
            raw_json=raw_json(raw),
            symbol=text(raw, "symbol"),
            side=_enum(raw, "side", _SIDES),
            qty=qty,
            price=price,
            order_id=text(raw, "order_id"),
            cum_qty=decimal(raw, "cum_qty"),
            leaves_qty=decimal(raw, "leaves_qty"),
            order_status=text(raw, "order_status"),
            fill_type=text(raw, "type"),
        )
    description = _activity_description(raw)
    return BrokerActivity(
        activity_id=activity_id,
        activity_type=activity_type,
        activity_date=calendar_date(raw, "date"),
        occurred_at=opt_timestamp(raw, "created_at"),
        raw_json=raw_json(raw),
        symbol=opt_text(raw, "symbol"),
        net_amount=decimal(raw, "net_amount"),
        order_id=opt_text(raw, "order_id"),
        qty=opt_decimal(raw, "qty"),
        per_share_amount=opt_decimal(raw, "per_share_amount"),
        description=description,
    )


_HHMM: Final = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def _clock_time(raw: Raw, key: str) -> time:
    value = text(raw, key)
    match = _HHMM.fullmatch(value)
    if match is None:
        raise InvalidBrokerDataError(f"field {key!r} is not an HH:MM time")
    return time(int(match[1]), int(match[2]))


def session_from_raw(raw: Raw) -> TradingSession:
    """A TRADING calendar row: date, open/close as America/New_York wall-clock HH:MM,
    optional settlement_date. Consistency checks happen when the fetch is recorded."""
    day = calendar_date(raw, "date")
    settles = None if raw.get("settlement_date") is None else calendar_date(raw, "settlement_date")
    return TradingSession(
        session_date=day,
        open_at=datetime.combine(day, _clock_time(raw, "open"), tzinfo=MARKET_TZ),
        close_at=datetime.combine(day, _clock_time(raw, "close"), tzinfo=MARKET_TZ),
        settlement_date=settles,
    )
