"""A synthetic weekday calendar for SIMULATION ledgers only (D14-4).

Every weekday is a 09:30-16:00 America/New_York session and a settlement day; there
are no holidays and no early closes. It is not a source of truth for anything. Its
fetches carry the source :data:`SIM_CALENDAR_SOURCE` and are loaded into a SimBroker,
which re-labels them ``sim``. ``CalendarStore.record_fetch`` rejects every source other
than ``alpaca`` for account-backed (observe) ledgers, so this calendar can never become
observe-mode evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarFetch, CalendarKind, TradingSession

SIM_CALENDAR_SOURCE: Final = "sim:synthetic-weekdays"


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _at(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=MARKET_TZ).astimezone(UTC)


def synthetic_calendars(
    start: date, end: date, *, fetched_at: datetime
) -> tuple[CalendarFetch, CalendarFetch]:
    """(TRADING, SETTLEMENT) fetches covering ``start``..``end`` inclusive."""
    days = _weekdays(start, end)
    sessions = tuple(
        TradingSession(
            session_date=d,
            open_at=_at(d, 9, 30),
            close_at=_at(d, 16, 0),
            settlement_date=_next_weekday(d),
        )
        for d in days
    )
    trading = CalendarFetch(
        CalendarKind.TRADING, start, end, fetched_at, SIM_CALENDAR_SOURCE, sessions=sessions
    )
    settlement = CalendarFetch(
        CalendarKind.SETTLEMENT,
        start,
        end,
        fetched_at,
        SIM_CALENDAR_SOURCE,
        settlement_days=tuple(days),
    )
    return trading, settlement
