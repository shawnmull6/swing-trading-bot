"""M2-2a review (Step 16.4A): daily-bar request boundaries, pagination and retries.

Alpaca's handling of ``start``/``end`` (inclusive vs exclusive; a bare date read as UTC or
market time) is not verified offline, so the adapter sends RFC 3339 bounds one second
before the relevant America/New_York midnights. These tests emulate a provider with
every inclusive/exclusive combination and check that exactly the requested sessions come
back — one session, several, Friday to Monday, both daylight-saving changes and an
early close. Pagination and read-retry failure paths never yield a partial result.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter, bar_window
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    InvalidBrokerDataError,
    ServiceUnavailableError,
)
from tests.support.alpaca_fake import FakeTransport, js
from tests.support.bars_fixture import AFTER_CLOSE, bars_json, history, sessions

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
        (date(2026, 11, 9), date(2026, 11, 9), "2026-11-09T04:59:59Z", "2026-11-10T04:59:59Z"),
        (date(2026, 7, 6), date(2026, 7, 6), "2026-07-06T03:59:59Z", "2026-07-07T03:59:59Z"),
        (date(2026, 10, 30), date(2026, 11, 2), "2026-10-30T03:59:59Z", "2026-11-03T04:59:59Z"),
        (date(2026, 3, 6), date(2026, 3, 9), "2026-03-06T04:59:59Z", "2026-03-10T03:59:59Z"),
        (date(2026, 11, 1), date(2026, 11, 1), "2026-11-01T03:59:59Z", "2026-11-02T04:59:59Z"),
    ],
)
def test_bounds_follow_market_time_offsets(start: date, end: date, lo: str, hi: str) -> None:
    assert bar_window(start, end) == (lo, hi)


def test_bounds_reject_inverted_range() -> None:
    with pytest.raises(ValueError, match="start"):
        bar_window(date(2026, 11, 10), date(2026, 11, 9))


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
    t, sleeps = FakeTransport(), []
    t.respond("data", "/stocks/bars", page(R[:2], "a"), ServiceUnavailableError("503"),
              page(R[2:], None))  # fmt: skip
    out = adapter(t, sleeps).get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")
    assert [b.session_date for b in out["AAPL"]] == STORE[-5:]
    assert sleeps == [timedelta(milliseconds=500)]
    assert t.calls[1][2] == t.calls[2][2]  # the same page request, same token


@pytest.mark.parametrize("error", [AuthenticationError("401", status_code=401),
                                   InvalidBrokerDataError("bad")])  # fmt: skip
def test_non_transient_errors_are_not_retried(error: Exception) -> None:
    t, sleeps = FakeTransport(), []
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
