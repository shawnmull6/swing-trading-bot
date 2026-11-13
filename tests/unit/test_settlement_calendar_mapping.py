"""Step 15B.5A: SETTLEMENT calendar rows -> settlement days.

Alpaca ``GET /v2/calendar?date_type=SETTLEMENT`` returns one row per **trade date**
(``date``) selected by its ``settlement_date`` falling in the requested range (Step 15B
live run #3). Settlement days are the distinct ``settlement_date`` values; the trade
``date`` is never a settlement day. Rows are validated strictly; range and weekend
problems are reported by ``CalendarFetch`` when the fetch is recorded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import settlement_days_from_raw
from swing_trading_bot.domain.calendar import MAX_SETTLEMENT_LAG_DAYS, CalendarFetch, CalendarKind

D = date


def row(trade: object, settles: object) -> dict[str, Any]:
    return {"date": trade, "open": "09:30", "close": "16:00", "session_open": "0400",
            "session_close": "2000", "settlement_date": settles}  # fmt: skip


def fetch(days: tuple[date, ...], start: date, end: date) -> CalendarFetch:
    at = datetime(2026, 11, 13, tzinfo=UTC)
    return CalendarFetch(CalendarKind.SETTLEMENT, start, end, at, "alpaca", settlement_days=days)


# ============================================================ valid rows
def test_settlement_days_are_the_settlement_dates_not_the_trade_dates() -> None:
    rows = [row("2026-10-30", "2026-11-02"), row("2026-11-02", "2026-11-03"),
            row("2026-11-03", "2026-11-04")]  # fmt: skip
    assert settlement_days_from_raw(rows) == (D(2026, 11, 2), D(2026, 11, 3), D(2026, 11, 4))


def test_duplicate_settlement_dates_are_collapsed() -> None:
    # Veterans Day 2026-11-11: a trading day that is not a settlement day.
    rows = [row("2026-11-10", "2026-11-12"), row("2026-11-11", "2026-11-12"),
            row("2026-11-12", "2026-11-13")]  # fmt: skip
    days = settlement_days_from_raw(rows)
    assert days == (D(2026, 11, 12), D(2026, 11, 13))
    assert D(2026, 11, 11) not in days


def test_output_is_sorted_whatever_the_row_order() -> None:
    rows = [row("2026-11-12", "2026-11-13"), row("2026-11-09", "2026-11-10")]
    assert settlement_days_from_raw(rows) == (D(2026, 11, 10), D(2026, 11, 13))


def test_no_rows_gives_no_days() -> None:
    assert settlement_days_from_raw([]) == ()


def test_lag_at_the_limit_is_accepted() -> None:
    trade = D(2026, 11, 2)
    settles = D(2026, 11, 2 + MAX_SETTLEMENT_LAG_DAYS)
    assert settlement_days_from_raw([row(trade.isoformat(), settles.isoformat())]) == (settles,)


# ============================================================ malformed rows
BAD: list[object] = [None, "", "2026/11/10", "Nov 10", "2026-13-01", 20261110, True, [], {}]


@pytest.mark.parametrize("value", BAD)
def test_malformed_trade_date_is_rejected(value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match="'date'"):
        settlement_days_from_raw([row(value, "2026-11-12")])


@pytest.mark.parametrize("value", BAD)
def test_malformed_settlement_date_is_rejected(value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match="settlement_date"):
        settlement_days_from_raw([row("2026-11-10", value)])


@pytest.mark.parametrize("key", ["date", "settlement_date"])
def test_missing_field_is_rejected(key: str) -> None:
    r = row("2026-11-10", "2026-11-12")
    del r[key]
    with pytest.raises(InvalidBrokerDataError, match=key):
        settlement_days_from_raw([r])


@pytest.mark.parametrize("settles", ["2026-11-10", "2026-11-09"])
def test_settlement_not_after_trade_is_rejected(settles: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match="not after"):
        settlement_days_from_raw([row("2026-11-10", settles)])


def test_excessive_lag_is_rejected() -> None:
    trade = D(2026, 11, 2)
    late = D(2026, 11, 2 + MAX_SETTLEMENT_LAG_DAYS + 1)
    with pytest.raises(InvalidBrokerDataError, match="lag"):
        settlement_days_from_raw([row(trade.isoformat(), late.isoformat())])


def test_one_bad_row_rejects_the_whole_response() -> None:
    rows = [row("2026-11-09", "2026-11-10"), row("2026-11-10", None)]
    with pytest.raises(InvalidBrokerDataError):
        settlement_days_from_raw(rows)


# ============================================================ fetch-level checks (unchanged)
def test_trade_dates_before_the_range_are_fine_when_settlement_is_inside() -> None:
    days = settlement_days_from_raw([row("2026-10-30", "2026-11-02")])
    assert fetch(days, D(2026, 11, 2), D(2026, 11, 6)).problems() == []


def test_settlement_date_outside_the_range_is_a_fetch_problem() -> None:
    days = settlement_days_from_raw([row("2026-11-06", "2026-11-09")])
    problems = fetch(days, D(2026, 11, 2), D(2026, 11, 6)).problems()
    assert problems == ["2026-11-09: settlement day outside requested range"]


def test_weekend_settlement_date_is_a_fetch_problem() -> None:
    days = settlement_days_from_raw([row("2026-11-06", "2026-11-07")])  # a Saturday
    problems = fetch(days, D(2026, 11, 2), D(2026, 11, 8)).problems()
    assert problems == ["2026-11-07: settlement day on a weekend"]


def test_range_boundaries_are_inclusive() -> None:
    days = settlement_days_from_raw([row("2026-10-30", "2026-11-02"),
                                     row("2026-11-05", "2026-11-06")])  # fmt: skip
    assert fetch(days, D(2026, 11, 2), D(2026, 11, 6)).problems() == []
