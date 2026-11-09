"""Deterministic offline calendar fixture (SYNTHETIC — not an authoritative calendar).

It plays the role of Alpaca's calendar in tests: explicit, hand-entered tables for
2025-12-01 .. 2027-12-31. It is test data only and must never be used for trading.

Provenance (step 6 review)
--------------------------
Settlement eligibility is encoded as an explicit table of **non-settlement weekdays**,
each tagged with its provenance:

* ``VERIFIED`` — confirmed from an authoritative published source (quoted below).
* ``ASSUMPTION`` — believed correct but NOT verified in this session; must be
  confirmed by the step 15 contract tests against Alpaca's SETTLEMENT calendar.

Verified from DTCC/NSCC Important Notices (retrieved 2026-11-09):

* 2026-04-03 Good Friday — NSCC A#9732 (2026-03-06): "No CNS activity will settle on
  Friday, April 3, 2026"; settlement of the prior day's activity is Monday, April 6.
  The notice also states U.S. Federal Banks are open that day, so bank schedules do
  NOT determine equity settlement eligibility.
* 2026-07-03 Independence Day observed — NSCC A#9778 (2026-06-05): "No CNS activity
  will settle on Friday, July 3, 2026"; settlement moves to Monday, July 6.
* 2026-06-19 Juneteenth — DTC B#24302-26 (2026-05-22): DTC "will be closed for
  business on Juneteenth, Friday, June 19, 2026".

Everything else — including Thanksgiving 2026, the Columbus Day / Veterans Day
"market open, no settlement" cases, all 2027 dates, and the baseline rule that every
other weekday is a settlement day — is an ASSUMPTION.

Exchange holidays and early closes follow the published NYSE schedule as understood
when writing this fixture; they too are confirmed only by the step 15 contract tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from swing_trading_bot.domain.calendar import (
    MARKET_TZ,
    CalendarFetch,
    CalendarKind,
    TradingSession,
)

FIXTURE_START = date(2025, 12, 1)
FIXTURE_END = date(2027, 12, 31)
FIXTURE_SOURCE = "fixture:synthetic-us-2026"


@dataclass(frozen=True, slots=True)
class Provenance:
    status: Literal["VERIFIED", "ASSUMPTION"]
    source: str


def _assumed(why: str) -> Provenance:
    return Provenance("ASSUMPTION", f"{why}; verify via Alpaca SETTLEMENT calendar (step 15)")


EXCHANGE_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),  # MLK
        date(2026, 2, 16),  # Presidents
        date(2026, 4, 3),  # Good Friday (markets closed: NSCC A#9732)
        date(2026, 5, 25),  # Memorial
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day observed (markets closed: NSCC A#9778)
        date(2026, 9, 7),  # Labor
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),
        date(2027, 6, 18),  # Juneteenth observed
        date(2027, 7, 5),  # Independence Day observed
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),  # Christmas observed
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

NON_SETTLEMENT_WEEKDAYS: dict[date, Provenance] = {
    date(2025, 12, 25): _assumed("Christmas"),
    # 2026
    date(2026, 1, 1): _assumed("New Year's Day"),
    date(2026, 1, 19): _assumed("MLK Day"),
    date(2026, 2, 16): _assumed("Presidents Day"),
    date(2026, 4, 3): Provenance(
        "VERIFIED",
        'NSCC Important Notice A#9732, 2026-03-06: "No CNS activity will settle on Friday, '
        'April 3, 2026"',
    ),
    date(2026, 5, 25): _assumed("Memorial Day"),
    date(2026, 6, 19): Provenance(
        "VERIFIED",
        "DTC Important Notice B#24302-26, 2026-05-22: DTC closed for business on Juneteenth",
    ),
    date(2026, 7, 3): Provenance(
        "VERIFIED",
        'NSCC Important Notice A#9778, 2026-06-05: "No CNS activity will settle on Friday, '
        'July 3, 2026"',
    ),
    date(2026, 9, 7): _assumed("Labor Day"),
    date(2026, 10, 12): _assumed("Columbus Day: equity market open, no settlement"),
    date(2026, 11, 11): _assumed("Veterans Day: equity market open, no settlement"),
    date(2026, 11, 26): _assumed("Thanksgiving"),
    date(2026, 12, 25): _assumed("Christmas"),
    # 2027
    date(2027, 1, 1): _assumed("New Year's Day"),
    date(2027, 1, 18): _assumed("MLK Day"),
    date(2027, 2, 15): _assumed("Presidents Day"),
    date(2027, 3, 26): _assumed("Good Friday (by analogy with 2026 NSCC A#9732)"),
    date(2027, 5, 31): _assumed("Memorial Day"),
    date(2027, 6, 18): _assumed("Juneteenth observed"),
    date(2027, 7, 5): _assumed("Independence Day observed"),
    date(2027, 9, 6): _assumed("Labor Day"),
    date(2027, 10, 11): _assumed("Columbus Day: equity market open, no settlement"),
    date(2027, 11, 11): _assumed("Veterans Day: equity market open, no settlement"),
    date(2027, 11, 25): _assumed("Thanksgiving"),
    date(2027, 12, 24): _assumed("Christmas observed"),
}

BASELINE_ASSUMPTION = _assumed("every other weekday in range is a settlement day")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def is_fixture_settlement_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NON_SETTLEMENT_WEEKDAYS


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
