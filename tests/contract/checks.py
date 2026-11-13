"""PYTEST_DONT_REWRITE

Read-only contract checks, shared by the offline replay and the supervised live run.

Each check returns a *sanitized* observation (booleans, small enumerations, type names)
or raises with a **fixed diagnostic code** only — never a value from a broker response:

* :class:`ContractViolation` — the response contradicts the contract the code relies on
  (row outcome FAILED);
* :class:`InsufficientEvidence` — the response was valid but cannot establish the
  contract (row outcome INCONCLUSIVE).

There are no bare ``assert`` statements (pytest assertion rewriting is also disabled for
this module), so a failure can never print broker data through introspection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from swing_trading_bot.app.broker_factory import account_identity_problems
from swing_trading_bot.broker.activity_feed import collect_window
from swing_trading_bot.broker.alpaca_http import ReadOnlyHttp
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import BrokerRequestError
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarKind

Observation = dict[str, Any]
ACCOUNT_MONEY_FIELDS = ("cash", "non_marginable_buying_power", "multiplier", "accrued_fees")
POSITION_NUMERIC_FIELDS = ("qty", "avg_entry_price", "market_value")
ORDER_NUMERIC_FIELDS = ("qty", "filled_qty", "limit_price")


class ContractViolation(Exception):  # noqa: N818 - an outcome, carries a fixed code only
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InsufficientEvidence(Exception):  # noqa: N818 - an outcome, carries a fixed code only
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractViolation(code)


@dataclass(frozen=True)
class ContractContext:
    adapter: AlpacaPaperReadAdapter
    http: ReadOnlyHttp
    now: datetime
    symbol: str = "SPY"
    calendar_start: date = date(2026, 11, 2)
    calendar_end: date = date(2026, 12, 31)


def check_account_identity(c: ContractContext) -> Observation:
    first, second = c.adapter.get_account(), c.adapter.get_account()
    problems = account_identity_problems(first, environment="paper")
    require(not problems, "ACCOUNT_IDENTITY_INVALID")
    require(first.account_id == second.account_id, "ACCOUNT_IDENTITY_UNSTABLE")
    return {"environment": first.environment, "id_is_canonical_uuid": True,
            "status_active": True, "currency_usd": True,
            "identity_stable_across_reads": True}  # fmt: skip


def _all_strings(rows: list[Any], fields: tuple[str, ...]) -> bool:
    return all(
        isinstance(row.get(f), str) for row in rows if isinstance(row, dict)
        for f in fields if row.get(f) is not None
    )  # fmt: skip


def check_raw_decimals(c: ContractContext) -> Observation:
    account = c.http.get_json("trading", "/account", {})
    if not isinstance(account, dict):
        raise ContractViolation("ACCOUNT_NOT_AN_OBJECT")
    present = [f for f in ACCOUNT_MONEY_FIELDS if f in account]
    require(bool(present), "ACCOUNT_MONEY_FIELDS_ABSENT")
    require(_all_strings([account], ACCOUNT_MONEY_FIELDS), "ACCOUNT_MONEY_NOT_STRINGS")
    positions = c.http.get_json("trading", "/positions", {})
    orders = c.http.get_json(
        "trading",
        "/orders",
        {"status": "all", "limit": "50", "direction": "desc", "nested": "false"},
    )
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ContractViolation("LIST_NOT_AN_ARRAY")
    require(_all_strings(positions, POSITION_NUMERIC_FIELDS), "POSITION_NUMERIC_NOT_STRINGS")
    require(_all_strings(orders, ORDER_NUMERIC_FIELDS), "ORDER_NUMERIC_NOT_STRINGS")
    return {
        "account_money_fields_are_strings": True,
        "fields_checked": sorted(present),
        "positions_present": bool(positions),
        "orders_present": bool(orders),
        "non_account_numerics_checked": bool(positions) or bool(orders),
        # Reported only; never used as proof of paper mode (undocumented).
        "account_number_prefix_is_PA": str(account.get("account_number", ""))[:2] == "PA",
    }


def check_account_restrictions(c: ContractContext) -> Observation:
    a, cfg = c.adapter.get_account(), c.adapter.get_account_config()
    return {
        "trading_blocked": a.trading_blocked,
        "account_blocked": a.account_blocked,
        "no_shorting": cfg.no_shorting,
        "max_margin_multiplier_is_1": cfg.max_margin_multiplier == 1,
        "suspend_trade": cfg.suspend_trade,
        "fractional_trading": cfg.fractional_trading,
        "m2_protections_in_place": cfg.m2_protections_in_place,
    }


def check_clock(c: ContractContext) -> Observation:
    clock = c.adapter.get_clock()
    require(all(t.utcoffset() is not None for t in (clock.timestamp, clock.next_open,
                                                    clock.next_close)), "CLOCK_NAIVE")  # fmt: skip
    day = clock.timestamp.astimezone(MARKET_TZ).date()
    trading = c.adapter.get_calendar(CalendarKind.TRADING, day - timedelta(days=7),
                                     day + timedelta(days=7))  # fmt: skip
    session = {s.session_date: s for s in trading.sessions}.get(day)
    inside = session is not None and session.open_at <= clock.timestamp < session.close_at
    require(clock.is_open == inside, "CLOCK_CALENDAR_DISAGREE")
    return {"timestamps_timezone_aware": True, "is_open_matches_calendar": True,
            "observed_state": "open" if clock.is_open else "closed"}  # fmt: skip


def check_calendars(c: ContractContext) -> Observation:
    from tests.support.calendar_fixture import (  # noqa: PLC0415
        fixture_sessions,
        fixture_settlement_days,
    )

    trading = c.adapter.get_calendar(CalendarKind.TRADING, c.calendar_start, c.calendar_end)
    settle = c.adapter.get_calendar(CalendarKind.SETTLEMENT, c.calendar_start, c.calendar_end)
    require(not trading.problems() and not settle.problems(), "CALENDAR_INCONSISTENT")
    require(bool(trading.sessions) and bool(settle.settlement_days), "CALENDAR_EMPTY")
    days = set(settle.settlement_days)
    populated = [s for s in trading.sessions if s.settlement_date is not None]
    in_range = [s for s in populated if s.settlement_date and s.settlement_date <= c.calendar_end]
    require(all(s.settlement_date in days for s in in_range), "SETTLEMENT_DATE_NOT_SETTLEMENT_DAY")
    want_sessions = {s.session_date for s in fixture_sessions(c.calendar_start, c.calendar_end)}
    want_days = set(fixture_settlement_days(c.calendar_start, c.calendar_end))
    got_sessions = {s.session_date for s in trading.sessions}
    return {
        "internally_consistent": True,
        "settlement_date_populated_share": round(len(populated) / len(trading.sessions), 3),
        "session_settlement_dates_are_settlement_days": True,
        "sessions_differing_from_fixture": sorted(str(d) for d in got_sessions ^ want_sessions),
        "settlement_days_differing_from_fixture": sorted(str(d) for d in days ^ want_days),
    }


def check_positions_and_orders(c: ContractContext) -> Observation:
    positions = c.adapter.get_positions()
    open_orders = c.adapter.get_open_orders()
    recent = c.adapter.get_orders(after=c.now - timedelta(days=1))
    return {"positions_parse": True, "open_orders_parse": True, "orders_list_parses": True,
            "account_flat": not positions and not open_orders,
            "order_list_semantics_exercised": bool(recent)}  # fmt: skip


def check_unknown_client_order_id(c: ContractContext) -> Observation:
    try:
        found = c.adapter.get_order_by_client_id("stb-contract-check-does-not-exist")
    except BrokerRequestError:
        raise ContractViolation("LOOKUP_STATUS_NOT_404") from None
    require(found is None, "UNKNOWN_CLIENT_ID_RETURNED_AN_ORDER")
    return {"unknown_client_order_id_is_none": True}


def check_activity_pagination(c: ContractContext) -> Observation:
    after, until = c.now - timedelta(days=30), c.now - timedelta(minutes=20)
    lag = timedelta(minutes=15)
    small = collect_window(c.adapter, after=after, until=until, now=c.now, posting_lag=lag,
                           page_size=10)  # fmt: skip
    large = collect_window(c.adapter, after=after, until=until, now=c.now, posting_lag=lag)
    mid = after + (until - after) / 2
    left = collect_window(c.adapter, after=after, until=mid, now=c.now, posting_lag=lag)
    right = collect_window(c.adapter, after=mid, until=until, now=c.now, posting_lag=lag)
    if not (small.tail_confirmed and large.tail_confirmed):
        raise InsufficientEvidence("ACTIVITY_TAIL_NOT_CONFIRMED")
    ids = [a.activity_id for a in large.activities]
    require({a.activity_id for a in small.activities} == set(ids), "PAGE_SIZE_CHANGED_THE_SET")
    split = {a.activity_id for a in (*left.activities, *right.activities)}
    require(split == set(ids), "ADJACENT_WINDOWS_DO_NOT_PARTITION")
    non_trade = [a for a in large.activities if not a.is_fill]
    require(all(a.net_amount is not None for a in non_trade), "NON_TRADE_WITHOUT_NET_AMOUNT")
    return {
        "tail_confirmed": True,
        "page_size_10_equals_100": True,
        "multi_page_exercised": len(ids) > 10,
        "ids_sorted_ascending": ids == sorted(ids),
        "adjacent_windows_partition_the_whole": True,
        "activity_types_seen": sorted({a.activity_type for a in large.activities}),
        "non_trade_have_net_amount": True,
    }


def check_market_data(c: ContractContext) -> Observation:
    marks = c.adapter.get_marks([c.symbol])
    if c.symbol not in marks:
        raise InsufficientEvidence("PROBE_SYMBOL_HAS_NO_LATEST_TRADE")
    mark = marks[c.symbol]
    require(mark.as_of.utcoffset() == timedelta(0), "LATEST_TRADE_NOT_UTC")
    raw = c.http.get_json("data", "/stocks/trades/latest",
                          {"symbols": c.symbol, "feed": mark.feed or "iex"})  # fmt: skip
    price = raw["trades"][c.symbol]["p"] if isinstance(raw, dict) else None
    require(isinstance(price, Decimal | int), "PRICE_NOT_EXACT")
    trading = c.adapter.get_calendar(CalendarKind.TRADING, c.now.date() - timedelta(days=10),
                                     c.now.date())  # fmt: skip
    closed = [s.session_date for s in trading.sessions if s.close_at < c.now]
    if not closed:
        raise InsufficientEvidence("NO_COMPLETED_SESSION_IN_RANGE")
    bar = c.adapter.get_daily_bar_closes([c.symbol], max(closed)).get(c.symbol)
    if bar is None:
        raise InsufficientEvidence("DAILY_BAR_ABSENT")
    require(bar.as_of.astimezone(MARKET_TZ).time() == datetime.min.time(), "BAR_NOT_MIDNIGHT_ET")
    return {"latest_trade_timestamp_utc": True, "price_decoded_exactly": True,
            "feed": mark.feed, "daily_bar_t_is_midnight_et": True}  # fmt: skip


CHECKS: dict[str, Callable[[ContractContext], Observation]] = {
    "account_identity": check_account_identity,
    "raw_decimals": check_raw_decimals,
    "account_restrictions": check_account_restrictions,
    "clock": check_clock,
    "calendars": check_calendars,
    "positions_and_orders": check_positions_and_orders,
    "unknown_client_order_id": check_unknown_client_order_id,
    "activity_pagination": check_activity_pagination,
    "market_data": check_market_data,
}
