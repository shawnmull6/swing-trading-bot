"""M2-2a review (Step 16.4A) and Step 16.5A: daily-bar request boundaries, pagination,
retries and data-host 403 handling.

Alpaca's handling of ``start``/``end`` (inclusive vs exclusive; a bare date read as UTC or
market time) is not verified, so the adapter sends RFC 3339 bounds one second before
``start``'s and one second after ``end``'s America/New_York midnight (Step 16.5A). These
tests emulate a provider with every inclusive/exclusive combination and check that
exactly the requested sessions come back — one session, several, Friday to Monday, a
weekend, both daylight-saving changes and an early close — and that the upper bound is
never in the future for a completed session. Pagination and read-retry failure paths
never yield a partial result.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.broker.alpaca_http import AlpacaReadTransport
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter, bar_window
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    DataEntitlementKind,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    ServiceUnavailableError,
)
from swing_trading_bot.config.settings import settings_from_mapping
from swing_trading_bot.domain.calendar import MARKET_TZ
from tests.support.alpaca_fake import FakeTransport, js
from tests.support.bars_fixture import (
    AFTER_CLOSE,
    AFTER_EARLY_CLOSE,
    bar_row,
    bars_json,
    calendar,
    history,
    sessions,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
STORE = sessions(date(2026, 2, 20), date(2026, 12, 10))
ROWS = history(STORE)

RANGES = {
    "one-session": (date(2026, 11, 9), date(2026, 11, 9)),
    "multi-session": (date(2026, 11, 9), date(2026, 11, 13)),
    "friday-to-monday": (date(2026, 11, 13), date(2026, 11, 16)),
    "dst-ends": (date(2026, 10, 30), date(2026, 11, 2)),  # EDT Fri -> EST Mon
    "dst-starts": (date(2026, 3, 6), date(2026, 3, 9)),  # EST Fri -> EDT Mon
    "early-close": (date(2026, 11, 27), date(2026, 11, 30)),
    "weekend-only": (date(2026, 11, 14), date(2026, 11, 15)),
}
SEMANTICS = [(True, True), (True, False), (False, True), (False, False)]


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def provider(*, start_inclusive: bool, end_inclusive: bool) -> Callable[[Mapping[str, str]], str]:
    """A provider that filters bars by ``t`` against the bounds with the given semantics."""

    def handler(params: Mapping[str, str]) -> str:
        lo, hi = parse(params["start"]), parse(params["end"])
        keep = []
        for row in ROWS:
            t = parse(row["t"])
            after = t >= lo if start_inclusive else t > lo
            before = t <= hi if end_inclusive else t < hi
            if after and before:
                keep.append(row)
        return bars_json({"bars": {"AAPL": keep} if keep else {}, "next_page_token": None})

    return handler


def adapter(t: FakeTransport, sleeps: list[timedelta] | None = None) -> AlpacaPaperReadAdapter:
    record = sleeps if sleeps is not None else []
    return AlpacaPaperReadAdapter(t, clock=lambda: AFTER_CLOSE, sleep=record.append)


def fetch(t: FakeTransport, start: date, end: date) -> list[date]:
    out = adapter(t).get_daily_bars(["AAPL"], start, end, feed="sip", adjustment="raw")
    return [b.session_date for b in out["AAPL"]]


# ============================================================ boundaries
@pytest.mark.parametrize("name", RANGES)
@pytest.mark.parametrize(("start_inclusive", "end_inclusive"), SEMANTICS)
def test_exactly_the_requested_sessions_under_any_bound_semantics(
    name: str, start_inclusive: bool, end_inclusive: bool
) -> None:
    start, end = RANGES[name]
    t = FakeTransport()
    t.always("data", "/stocks/bars",
             provider(start_inclusive=start_inclusive, end_inclusive=end_inclusive))  # fmt: skip
    assert fetch(t, start, end) == sessions(start, end)


def test_single_session_requests_agree_with_one_multi_session_request() -> None:
    start, end = date(2026, 10, 26), date(2026, 11, 6)  # spans the DST change
    t = FakeTransport()
    t.always("data", "/stocks/bars", provider(start_inclusive=True, end_inclusive=False))
    singles = [d for day in sessions(start, end) for d in fetch(t, day, day)]
    assert singles == fetch(t, start, end) == sessions(start, end)


@pytest.mark.parametrize(
    ("start", "end", "lo", "hi"),
    [
        (date(2026, 11, 9), date(2026, 11, 9), "2026-11-09T04:59:59Z", "2026-11-09T05:00:01Z"),
        (date(2026, 7, 6), date(2026, 7, 6), "2026-07-06T03:59:59Z", "2026-07-06T04:00:01Z"),
        (date(2026, 10, 30), date(2026, 11, 2), "2026-10-30T03:59:59Z", "2026-11-02T05:00:01Z"),
        (date(2026, 3, 6), date(2026, 3, 9), "2026-03-06T04:59:59Z", "2026-03-09T04:00:01Z"),
        (date(2026, 11, 1), date(2026, 11, 1), "2026-11-01T03:59:59Z", "2026-11-01T04:00:01Z"),
        (date(2026, 3, 8), date(2026, 3, 8), "2026-03-08T04:59:59Z", "2026-03-08T05:00:01Z"),
        (date(2026, 11, 14), date(2026, 11, 15), "2026-11-14T04:59:59Z", "2026-11-15T05:00:01Z"),
    ],
)
def test_bounds_follow_market_time_offsets(start: date, end: date, lo: str, hi: str) -> None:
    """Each bound converts its own date's midnight: EDT (UTC-4) vs EST (UTC-5), and both
    daylight-saving Sundays (the change is at 02:00, so midnight keeps the old offset)."""
    assert bar_window(start, end) == (lo, hi)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=MARKET_TZ)


@given(st.dates(date(2000, 1, 1), date(2040, 12, 31)), st.integers(0, 400))
def test_bounds_bracket_exactly_the_bar_timestamps(start: date, span: int) -> None:
    end = start + timedelta(days=span)
    lo, hi = (parse(x) for x in bar_window(start, end))
    assert lo == midnight(start) - timedelta(seconds=1)
    assert hi == midnight(end) + timedelta(seconds=1)
    # neighbours are at least 23 h away (a 23-hour DST day is the shortest)
    assert midnight(start - timedelta(days=1)) <= lo - timedelta(hours=22)
    assert midnight(end + timedelta(days=1)) >= hi + timedelta(hours=22)


@given(st.dates(date(2000, 1, 1), date(2040, 12, 31)), st.integers(16 * 60, 3 * 24 * 60))
def test_upper_bound_is_never_in_the_future_for_a_completed_session(
    end: date, minutes_after_midnight: int
) -> None:
    """Any time from the regular close (16:00 ET) onwards; an early close is earlier
    still, and the bound is one second after the session's midnight in every case."""
    now = midnight(end) + timedelta(minutes=minutes_after_midnight)
    _, hi = bar_window(end - timedelta(days=4), end)
    assert parse(hi) < now


def test_bounds_reject_inverted_range() -> None:
    with pytest.raises(ValueError, match="start"):
        bar_window(date(2026, 11, 10), date(2026, 11, 9))


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        (AFTER_CLOSE, date(2026, 11, 30), date(2026, 11, 30)),  # just after the close
        (AFTER_EARLY_CLOSE, date(2026, 11, 23), date(2026, 11, 27)),  # 13:30 ET early close
        (datetime(2026, 11, 14, 1, 30, 8, tzinfo=UTC), date(2026, 11, 9), date(2026, 11, 13)),
        (datetime(2026, 11, 13, 21, 0, 1, tzinfo=UTC), date(2026, 11, 13), date(2026, 11, 13)),
    ],
)
def test_requests_near_the_current_time_stay_in_the_past(
    now: datetime, start: date, end: date
) -> None:
    t = FakeTransport()
    t.always("data", "/stocks/bars", provider(start_inclusive=True, end_inclusive=True))
    out = AlpacaPaperReadAdapter(t, clock=lambda: now).get_daily_bars(
        ["AAPL"], start, end, feed="sip", adjustment="raw")  # fmt: skip
    assert [b.session_date for b in out["AAPL"]] == sessions(start, end)
    assert parse(t.calls[0][2]["end"]) < now
    cal = calendar(now)  # the last requested session has closed by ``now``
    assert cal.session(end).close_at <= now


def test_an_unfinished_session_is_requested_but_never_judged_complete() -> None:
    """Asking for today's session while it trades: the bound is in the past (today's
    midnight + 1 s), the provider may return a partial bar, and history assessment
    excludes it (bar_history), exactly as before Step 16.5A."""
    from swing_trading_bot.broker.bar_history import HistoryRequirement, assess_history  # noqa: PLC0415
    from tests.support.bars_fixture import IN_SESSION  # noqa: PLC0415

    today = date(2026, 11, 30)
    t = FakeTransport()
    t.always("data", "/stocks/bars", provider(start_inclusive=True, end_inclusive=True))
    bars = AlpacaPaperReadAdapter(t, clock=lambda: IN_SESSION).get_daily_bars(
        ["AAPL"], date(2026, 11, 23), today, feed="sip", adjustment="raw")["AAPL"]  # fmt: skip
    assert parse(t.calls[0][2]["end"]) < IN_SESSION and bars[-1].session_date == today
    result = assess_history("AAPL", bars, calendar=calendar(IN_SESSION), now=IN_SESSION,
                            requirement=HistoryRequirement(4, "sip", "raw"))  # fmt: skip
    assert today in result.excluded and today not in result.window


def test_c8_close_lookup_keeps_its_date_only_request() -> None:
    t = FakeTransport()
    t.respond("data", "/stocks/bars", js({"bars": {}, "next_page_token": None}))
    adapter(t).get_daily_bar_closes(["AAPL"], date(2026, 11, 9))
    assert (t.calls[0][2]["start"], t.calls[0][2]["end"]) == ("2026-11-09", "2026-11-09")


# ============================================================ pagination
def page(rows: Sequence[dict[str, Any]], token: str | None) -> str:
    return bars_json({"bars": {"AAPL": list(rows)} if rows else {}, "next_page_token": token})


R = ROWS[-5:]
START, END = STORE[-5], STORE[-1]


def test_all_pages_are_collected_before_returning() -> None:
    t = FakeTransport()
    t.respond("data", "/stocks/bars", page(R[:2], "a"), page(R[2:4], "b"), page(R[4:], None))
    assert fetch(t, START, END) == STORE[-5:]
    assert [c[2].get("page_token") for c in t.calls] == [None, "a", "b"]


@pytest.mark.parametrize(
    "tokens",
    [["a", ""], ["a", "a"], ["a", "b", "a"], ["a", 7], ["a", ["b"]]],
    ids=["empty", "repeated", "cycle", "number", "list"],
)
def test_bad_page_tokens_fail_closed_without_a_result(tokens: list[object]) -> None:
    t = FakeTransport()
    bodies = [bars_json({"bars": {}, "next_page_token": tok}) for tok in tokens]
    t.respond("data", "/stocks/bars", *bodies)
    with pytest.raises(InvalidBrokerDataError, match="next_page_token"):
        fetch(t, START, END)


def test_failure_on_a_later_page_returns_nothing() -> None:
    t = FakeTransport()
    t.respond("data", "/stocks/bars", page(R[:2], "a"), *[ServiceUnavailableError("503")] * 3)
    with pytest.raises(ServiceUnavailableError):
        fetch(t, START, END)
    assert len(t.calls) == 4  # page 1 + 3 bounded attempts at page 2


def test_malformed_later_page_returns_nothing() -> None:
    t = FakeTransport()
    bad = bars_json({"bars": {"AAPL": [{**R[2], "v": 1.5}]}, "next_page_token": None})
    t.respond("data", "/stocks/bars", page(R[:2], "a"), bad)
    with pytest.raises(InvalidBrokerDataError, match="JSON integer"):
        fetch(t, START, END)


# ============================================================ read retries
def test_transient_page_error_is_retried_within_bounds() -> None:
    t, sleeps = FakeTransport(), list[timedelta]()
    t.respond("data", "/stocks/bars", page(R[:2], "a"), ServiceUnavailableError("503"),
              page(R[2:], None))  # fmt: skip
    out = adapter(t, sleeps).get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")
    assert [b.session_date for b in out["AAPL"]] == STORE[-5:]
    assert sleeps == [timedelta(milliseconds=500)]
    assert t.calls[1][2] == t.calls[2][2]  # the same page request, same token


@pytest.mark.parametrize("error", [AuthenticationError("401", status_code=401),
                                   InvalidBrokerDataError("bad")])  # fmt: skip
def test_non_transient_errors_are_not_retried(error: Exception) -> None:
    t, sleeps = FakeTransport(), list[timedelta]()
    t.respond("data", "/stocks/bars", error)
    with pytest.raises(type(error)):
        adapter(t, sleeps).get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")
    assert len(t.calls) == 1 and sleeps == []


def test_read_retries_never_reach_an_order_write_path() -> None:
    """Retries live only in the GET-only read stack; the execution capability (the only
    code that calls submit_order/cancel_order) never imports it."""
    users = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "with_read_retries" in path.read_text() or "DEFAULT_READ_POLICIES" in path.read_text()
    }
    assert users == {"broker/alpaca_http.py", "broker/alpaca_read.py"}
    tree = ast.parse((SRC / "oms" / "execution.py").read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m and "alpaca" in m}


# ============================================================ Step 16.5 replay
FAKE_SETTINGS = settings_from_mapping({"ALPACA_API_KEY": "PKFAKETESTKEY000000001",
                                       "ALPACA_SECRET_KEY": "fakeTestSecret/0000000000001",
                                       "TRADING_MODE": "paper"})  # fmt: skip
CORE = ["AAPL", "MSFT", "SPY"]
STEP_16_5_SENT = datetime(2026, 11, 14, 1, 30, 8, tzinfo=UTC)
RECENT_SIP_403 = '{"message": "subscription does not permit querying recent SIP data"}'


def http(status: int, body: str) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    return r


class RecentWindowSession:
    """A ``requests.Session`` stand-in for the data host that refuses, with the
    (publicly reported, not live-captured) recent-SIP 403, any SIP request whose ``end``
    lies after ``now`` — the Step 16.5 hypothesis — and otherwise serves synthetic bars
    shaped like the Step 16.5 response (integer ``c``/``l`` for a whole price)."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        params = kwargs["params"]
        if params["feed"] == "sip" and parse(params["end"]) > self.now:
            return http(403, RECENT_SIP_403)
        lo, hi = parse(params["start"]), parse(params["end"])
        bars: dict[str, list[dict[str, Any]]] = {}
        for i, symbol in enumerate(params["symbols"].split(",")):
            for day in sessions(lo.astimezone(MARKET_TZ).date(), hi.astimezone(MARKET_TZ).date()):
                if lo < midnight(day) < hi:
                    row = bar_row(day, Decimal(100 * (i + 1)))
                    if symbol == "MSFT":  # whole-number prices arrive as JSON integers
                        row.update(l=int(row["o"]) - 1, c=int(row["o"]))
                    bars.setdefault(symbol, []).append(row)
        return http(200, bars_json({"bars": bars, "next_page_token": None}))


def replay(now: datetime, start: date, end: date) -> tuple[Any, RecentWindowSession]:
    session = RecentWindowSession(now)
    transport = AlpacaReadTransport(FAKE_SETTINGS, session=session)  # type: ignore[arg-type]
    sleeps: list[timedelta] = []
    read = AlpacaPaperReadAdapter(transport, clock=lambda: now, sleep=sleeps.append)
    try:
        out: Any = read.get_daily_bars(CORE, start, end, feed="sip", adjustment="raw")
    except MarketDataEntitlementError as exc:
        out = exc
    assert sleeps == []  # nothing in this path is ever retried
    return out, session


def test_step_16_5_successful_request_with_corrected_bounds() -> None:
    out, session = replay(STEP_16_5_SENT, date(2026, 11, 12), date(2026, 11, 12))
    (call,) = session.calls
    assert call["url"] == "https://data.alpaca.markets/v2/stocks/bars"
    assert call["params"] == {
        "symbols": "AAPL,MSFT,SPY", "timeframe": "1Day",
        "start": "2026-11-12T04:59:59Z", "end": "2026-11-12T05:00:01Z",  # end was 11-13T04:59:59Z
        "feed": "sip", "adjustment": "raw", "limit": "10000", "sort": "asc",
    }  # fmt: skip
    assert {s: [b.session_date for b in out[s]] for s in CORE} == {
        s: [date(2026, 11, 12)] for s in CORE}  # fmt: skip
    msft = out["MSFT"][0]
    assert (msft.close, msft.low) == (Decimal(200), Decimal(199))
    assert all(b.as_of.isoformat() == "2026-11-12T05:00:00+00:00" for s in CORE for b in out[s])


def test_step_16_5_failed_request_now_stays_in_the_past() -> None:
    """Request 2 of Step 16.5 (2026-11-09..11-13 at 01:30:08Z) sent
    end=2026-11-14T04:59:59Z, ~3.5 h ahead, and got 403. The corrected end is
    2026-11-13T05:00:01Z, so this emulated recent-window provider serves it."""
    out, session = replay(STEP_16_5_SENT, date(2026, 11, 9), date(2026, 11, 13))
    (call,) = session.calls
    assert (call["params"]["start"], call["params"]["end"]) == (
        "2026-11-09T04:59:59Z", "2026-11-13T05:00:01Z")  # fmt: skip
    assert parse("2026-11-14T04:59:59Z") > STEP_16_5_SENT > parse(call["params"]["end"])
    assert {s: [b.session_date for b in out[s]] for s in CORE} == {
        s: sessions(date(2026, 11, 9), date(2026, 11, 13)) for s in CORE}  # fmt: skip


def test_step_16_5_style_403_is_an_entitlement_error_not_an_auth_failure() -> None:
    """Asking for today's (unfinished) session moments after midnight still reaches past
    ``now``; the recognized 403 blocks the read once, with no retry and no AUTH."""
    now = midnight(date(2026, 11, 16))  # exactly at the bar timestamp: end = now + 1 s
    out, session = replay(now, date(2026, 11, 16), date(2026, 11, 16))
    assert isinstance(out, MarketDataEntitlementError)
    assert AuthenticationError not in type(out).__mro__
    assert (out.kind, out.feed) == (DataEntitlementKind.RECENT_DATA_WINDOW, "sip")
    assert len(session.calls) == 1
