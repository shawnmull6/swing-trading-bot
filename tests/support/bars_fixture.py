"""Deterministic historical daily-bar fixtures (M2-2a), offline and synthetic.

Rows have the field names and JSON types of Alpaca ``/v2/stocks/bars`` daily bars
(``t`` = the session's midnight America/New_York in UTC, ``o``/``h``/``l``/``c``/``vw``
numbers, ``v``/``n`` integers). Prices are deterministic functions of the session index;
no real market value is reproduced. Sessions come from the fixture trading calendar,
which includes early closes (e.g. 2026-11-27).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarData, TradingCalendar
from tests.support.calendar_fixture import fixture_sessions, settlement_fetch, trading_fetch

D = Decimal
Row = dict[str, Any]
#: After the close of Friday 2026-11-27 (an early-close session, 13:00 ET).
AFTER_EARLY_CLOSE = datetime(2026, 11, 27, 13, 30, tzinfo=MARKET_TZ).astimezone(UTC)
#: Monday 2026-11-30, 10:00 ET: inside a session (its bar is incomplete).
IN_SESSION = datetime(2026, 11, 30, 10, 0, tzinfo=MARKET_TZ).astimezone(UTC)
#: Monday 2026-11-30, 17:00 ET: after that session's close.
AFTER_CLOSE = datetime(2026, 11, 30, 17, 0, tzinfo=MARKET_TZ).astimezone(UTC)
SPLIT_DAY = date(2026, 11, 18)  # synthetic 2-for-1 split effective this session


def calendar(as_of: datetime) -> TradingCalendar:
    data = CalendarData().with_fetch(trading_fetch(as_of)).with_fetch(settlement_fetch(as_of))
    return TradingCalendar(data, as_of=as_of, max_age=timedelta(hours=36))


def sessions(start: date, end: date) -> list[date]:
    return [s.session_date for s in fixture_sessions(start, end)]


def t_of(day: date) -> str:
    midnight = datetime.combine(day, time(0, 0), tzinfo=MARKET_TZ).astimezone(UTC)
    return midnight.isoformat().replace("+00:00", "Z")


def bar_row(day: date, base: Decimal, *, volume: int = 1_000_000, optional: bool = True) -> Row:
    """A consistent bar around ``base``: low < open, close < high."""
    row: Row = {
        "t": t_of(day),
        "o": base,
        "h": base + D("1.25"),
        "l": base - D("0.75"),
        "c": base + D("0.5001"),
        "v": volume,
    }
    if optional:
        row["vw"] = base + D("0.1234")
        row["n"] = 5_000
    return row


def history(
    days: Iterable[date], *, start_price: str = "100.00", step: str = "0.10", **kw: Any
) -> list[Row]:
    return [bar_row(d, D(start_price) + i * D(step), **kw) for i, d in enumerate(days)]


def split_pair(days: Sequence[date], *, ratio: int = 2) -> tuple[list[Row], list[Row]]:
    """(raw, split-adjusted) histories for a ``ratio``-for-1 split on :data:`SPLIT_DAY`:
    raw prices before the split are ``ratio`` times the adjusted ones (volume divided)."""
    adjusted = history(days, start_price="50.00", step="0.05")
    raw = []
    for row in adjusted:
        r = dict(row)
        day = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(MARKET_TZ)
        if day.date() < SPLIT_DAY:
            for k in ("o", "h", "l", "c", "vw"):
                r[k] = row[k] * ratio
            r["v"] = row["v"] // ratio
        raw.append(r)
    return raw, adjusted


# ---------------------------------------------------------------- JSON bodies
_NUM = re.compile(r'"__num__(-?[0-9.Ee+-]+)__"')


def bars_json(payload: Mapping[str, Any]) -> str:
    """Serialize with Decimals as exact JSON numbers (as Alpaca sends them)."""

    def encode(value: object) -> object:
        if isinstance(value, Decimal):
            return f"__num__{value}__"
        raise TypeError(type(value).__name__)

    return _NUM.sub(r"\1", json.dumps(payload, default=encode))


def paged(
    rows_by_symbol: Mapping[str, Sequence[Row]], page_size: int
) -> Callable[[Mapping[str, str]], str]:
    """A FakeTransport handler serving ``/stocks/bars`` like Alpaca: symbols in request
    order, each symbol's rows in order, ``page_size`` rows per page across symbols,
    ``next_page_token`` until exhausted. Records nothing; the transport records calls."""
    flat = [(s, r) for s, rows in rows_by_symbol.items() for r in rows]

    def handler(params: Mapping[str, str]) -> str:
        offset = int(params.get("page_token", "tok-0").removeprefix("tok-"))
        chunk = flat[offset : offset + page_size]
        bars: dict[str, list[Row]] = {}
        for symbol, row in chunk:
            bars.setdefault(symbol, []).append(row)
        nxt = offset + page_size
        token = f"tok-{nxt}" if nxt < len(flat) else None
        return bars_json({"bars": bars, "next_page_token": token})

    return handler
