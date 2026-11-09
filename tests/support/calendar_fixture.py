"""Deterministic offline calendar fixture (SYNTHETIC — not an authoritative calendar).

It plays the role of Alpaca's calendar in tests: explicit, hand-entered tables for
2025-12-01 .. 2027-12-31. Values follow published NYSE schedules and Federal Reserve
holiday rules closely enough to exercise every rule in ARCHITECTURE §6.3, but tests
must never treat this module as a source of truth for real trading.

Conventions:
* Exchange holidays: no session. Early closes: 13:00 ET.
* Settlement days: weekdays that are not Federal Reserve holidays. Fed rule: a
  holiday on Sunday is observed Monday; a holiday on Saturday is NOT observed (banks
  open Friday) — so e.g. 2026-07-03 is an exchange holiday but a settlement day.
* Each session's ``settlement_date`` is the first settlement day after it (T+1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from swing_trading_bot.domain.calendar import (
    MARKET_TZ,
    CalendarFetch,
    CalendarKind,
    TradingSession,
)

FIXTURE_START = date(2025, 12, 1)
FIXTURE_END = date(2027, 12, 31)
FIXTURE_SOURCE = "fixture:synthetic-us-2026"

EXCHANGE_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),  # MLK
        date(2026, 2, 16),  # Presidents
        date(2026, 4, 3),  # Good Friday (banks open)
        date(2026, 5, 25),  # Memorial
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day observed (Jul 4 is Saturday; banks open)
        date(2026, 9, 7),  # Labor
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),
        date(2027, 6, 18),  # Juneteenth observed (Jun 19 is Saturday; banks open)
        date(2027, 7, 5),  # Independence Day observed (Jul 4 is Sunday; banks closed)
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),  # Christmas observed (Dec 25 is Saturday; banks open)
    }
)

EARLY_CLOSES: frozenset[date] = frozenset(
    {
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
    }
)

BANK_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 9, 7),
        date(2026, 10, 12),  # Columbus Day: market OPEN, banks closed
        date(2026, 11, 11),  # Veterans Day: market OPEN, banks closed
        date(2026, 11, 26),
        date(2026, 12, 25),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 5, 31),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 10, 11),  # Columbus Day
        date(2027, 11, 11),  # Veterans Day
        date(2027, 11, 25),
    }
)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def is_fixture_settlement_day(d: date) -> bool:
    return d.weekday() < 5 and d not in BANK_HOLIDAYS


def fixture_settlement_days(start: date = FIXTURE_START, end: date = FIXTURE_END) -> list[date]:
    return [d for d in _days(start, end) if is_fixture_settlement_day(d)]


def _next_settlement_day(d: date) -> date:
    candidate = d + timedelta(days=1)
    while not is_fixture_settlement_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def fixture_sessions(
    start: date = FIXTURE_START,
    end: date = FIXTURE_END,
    *,
    with_settlement_dates: bool = True,
) -> list[TradingSession]:
    sessions = []
    for d in _days(start, end):
        if d.weekday() >= 5 or d in EXCHANGE_HOLIDAYS:
            continue
        close = time(13, 0) if d in EARLY_CLOSES else time(16, 0)
        sessions.append(
            TradingSession(
                session_date=d,
                open_at=datetime.combine(d, time(9, 30), tzinfo=MARKET_TZ).astimezone(UTC),
                close_at=datetime.combine(d, close, tzinfo=MARKET_TZ).astimezone(UTC),
                settlement_date=_next_settlement_day(d) if with_settlement_dates else None,
            )
        )
    return sessions


def trading_fetch(
    fetched_at: datetime,
    start: date = FIXTURE_START,
    end: date = FIXTURE_END,
    *,
    with_settlement_dates: bool = True,
) -> CalendarFetch:
    return CalendarFetch(
        kind=CalendarKind.TRADING,
        start=start,
        end=end,
        fetched_at=fetched_at,
        source=FIXTURE_SOURCE,
        sessions=tuple(fixture_sessions(start, end, with_settlement_dates=with_settlement_dates)),
    )


def settlement_fetch(
    fetched_at: datetime, start: date = FIXTURE_START, end: date = FIXTURE_END
) -> CalendarFetch:
    return CalendarFetch(
        kind=CalendarKind.SETTLEMENT,
        start=start,
        end=end,
        fetched_at=fetched_at,
        source=FIXTURE_SOURCE,
        settlement_days=tuple(fixture_settlement_days(start, end)),
    )
