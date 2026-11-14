"""M2-2a: historical daily bars through the Alpaca read adapter (fake transport), the
SimBroker, and the calendar-aware history validator. Offline and synthetic only.

Also pins what M2-2a must not change: the C8 ``get_daily_bar_closes`` path, the
unsupported official closes, OFFICIAL-only observe baselines, and observe-mode commands
that never request bars.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.bar_history import (
    HistoryIssue,
    HistoryRequirement,
    HistoryStatus,
    assess_histories,
    assess_history,
    last_completed_session,
    required_window,
)
from swing_trading_bot.broker.errors import InvalidBrokerDataError, UnsupportedBrokerOperationError
from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.broker.ports import BarSource, BrokerReadPort, PriceMarkSource
from swing_trading_bot.broker.sim import SimBroker, SimClock
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarData, TradingCalendar
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.loss_limit_store import CloseSource
from tests.support.alpaca_fake import FakeTransport, js
from tests.support.bars_fixture import (
    AFTER_CLOSE,
    AFTER_EARLY_CLOSE,
    IN_SESSION,
    SPLIT_DAY,
    bar_row,
    bars_json,
    calendar,
    history,
    paged,
    sessions,
    split_pair,
)
from tests.support.calendar_fixture import trading_fetch
from tests.support.cli_env import CliEnv, cli_env

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
START, END = date(2026, 10, 1), date(2026, 11, 30)
ALL = sessions(START, END)
SIP_RAW = HistoryRequirement(lookback_sessions=20, feed="sip", adjustment="raw")


def adapter(now: datetime = AFTER_CLOSE) -> tuple[AlpacaPaperReadAdapter, FakeTransport]:
    t = FakeTransport()
    return AlpacaPaperReadAdapter(t, clock=lambda: now, sleep=lambda _d: None), t


def sim(now: datetime = AFTER_CLOSE) -> SimBroker:
    return SimBroker(SimClock(now))


# ============================================================ interface
def test_both_implementations_are_bar_sources() -> None:
    a, _ = adapter()
    assert isinstance(a, BarSource) and isinstance(a, BrokerReadPort)
    assert isinstance(a, PriceMarkSource)
    assert isinstance(sim(), BarSource)


# ============================================================ Alpaca adapter
def test_request_carries_explicit_feed_adjustment_and_range() -> None:
    a, t = adapter()
    t.respond("data", "/stocks/bars", js({"bars": {}, "next_page_token": None}))
    out = a.get_daily_bars(["AAPL", "MSFT"], START, END, feed="sip", adjustment="split")
    assert out == {"AAPL": (), "MSFT": ()}  # every requested symbol, none invented
    (service, path, params), = t.calls  # fmt: skip
    assert (service, path) == ("data", "/stocks/bars")
    assert params == {"symbols": "AAPL,MSFT", "timeframe": "1Day",
                      "start": "2026-10-01T03:59:59Z",  # 1 s before 2026-10-01 00:00 EDT
                      "end": "2026-12-01T04:59:59Z",  # 1 s before 2026-12-01 00:00 EST
                      "feed": "sip", "adjustment": "split",
                      "limit": "10000", "sort": "asc"}  # fmt: skip


@pytest.mark.parametrize(("feed", "adjustment"), [("iex", "raw"), ("sip", "raw"),
                                                  ("iex", "split"), ("sip", "split")])  # fmt: skip
def test_feed_and_adjustment_propagate_to_every_bar(feed: str, adjustment: str) -> None:
    a, t = adapter()
    t.always("data", "/stocks/bars", paged({"AAPL": history(ALL[:3])}, page_size=2))
    bars = a.get_daily_bars(["AAPL"], START, END, feed=feed, adjustment=adjustment)  # type: ignore[arg-type]
    assert len(bars["AAPL"]) == 3
    assert {(b.feed, b.adjustment) for b in bars["AAPL"]} == {(feed, adjustment)}
    assert {c[2]["feed"] for c in t.calls} == {feed}
    assert {c[2]["adjustment"] for c in t.calls} == {adjustment}


@pytest.mark.parametrize("page_size", [5, 7, 40, 10_000])  # <= 20 pages (the cap)
def test_multi_symbol_pagination_is_page_size_independent(page_size: int) -> None:
    data = {"AAPL": history(ALL), "MSFT": history(ALL[5:], start_price="400"),
            "SPY": history(ALL[:2], start_price="600")}  # fmt: skip
    a, t = adapter()
    t.always("data", "/stocks/bars", paged(data, page_size))
    out = a.get_daily_bars(["AAPL", "MSFT", "SPY"], START, END, feed="iex", adjustment="raw")
    assert {s: len(b) for s, b in out.items()} == {"AAPL": len(ALL), "MSFT": len(ALL) - 5,
                                                    "SPY": 2}  # fmt: skip
    for series in out.values():
        days = [b.session_date for b in series]
        assert days == sorted(set(days))
    pages = -(-sum(len(v) for v in data.values()) // page_size)
    assert len(t.calls) == pages
    assert [c[2].get("page_token") for c in t.calls][1:] == [
        f"tok-{i * page_size}" for i in range(1, pages)]  # fmt: skip


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"bars": {"TSLA": [bar_row(ALL[0], D(10))]}, "next_page_token": None}, "unrequested"),
        ({"bars": {"AAPL": [bar_row(ALL[0], D(10)), bar_row(ALL[0], D(11))]},
          "next_page_token": None}, "duplicate"),
        ({"bars": {"AAPL": [bar_row(ALL[1], D(10)), bar_row(ALL[0], D(11))]},
          "next_page_token": None}, "out-of-order"),
        ({"bars": {"AAPL": [bar_row(date(2026, 9, 30), D(10))]}, "next_page_token": None},
         "outside"),
        ({"bars": {"AAPL": [{**bar_row(ALL[0], D(10)), "h": D(1)}]}, "next_page_token": None},
         "OHLC"),
        ({"bars": {"AAPL": {"t": "x"}}, "next_page_token": None}, "expected a JSON array"),
        ({"bars": [1], "next_page_token": None}, "expected a JSON object"),
        ({"bars": {}, "next_page_token": 5}, "next_page_token"),
    ],
)  # fmt: skip
def test_malformed_responses_fail_closed(body: dict[str, Any], message: str) -> None:
    a, t = adapter()
    t.respond("data", "/stocks/bars", bars_json(body))
    with pytest.raises(InvalidBrokerDataError, match=message):
        a.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")


def test_duplicate_across_pages_and_repeated_token_fail_closed() -> None:
    a, t = adapter()
    page = bars_json({"bars": {"AAPL": [bar_row(ALL[0], D(10))]}, "next_page_token": "x"})
    again = bars_json({"bars": {"AAPL": [bar_row(ALL[0], D(10))]}, "next_page_token": None})
    t.respond("data", "/stocks/bars", page, again)
    with pytest.raises(InvalidBrokerDataError, match="duplicate"):
        a.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")
    empty = bars_json({"bars": {}, "next_page_token": "x"})
    t.respond("data", "/stocks/bars", empty, empty)
    with pytest.raises(InvalidBrokerDataError, match="next_page_token"):
        a.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")


def test_endless_pagination_is_capped() -> None:
    a, t = adapter()

    def endless(p: dict[str, str]) -> str:
        return js({"bars": {}, "next_page_token": p.get("page_token", "") + "x"})

    t.always("data", "/stocks/bars", endless)
    with pytest.raises(InvalidBrokerDataError, match="exceeded 20 pages"):
        a.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")
    assert len(t.calls) == 20


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"feed": "delayed_sip", "adjustment": "raw"}, "feed"),
        ({"feed": "iex", "adjustment": "all"}, "adjustment"),
    ],
)
def test_unsupported_parameters_rejected_before_any_request(kwargs: dict[str, str],
                                                            message: str) -> None:  # fmt: skip
    a, t = adapter()
    with pytest.raises(ValueError, match=message):
        a.get_daily_bars(["AAPL"], START, END, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="start"):
        a.get_daily_bars(["AAPL"], END, START, feed="iex", adjustment="raw")
    with pytest.raises(ValueError, match="invalid symbol"):
        a.get_daily_bars(["aapl"], START, END, feed="iex", adjustment="raw")
    assert a.get_daily_bars([], START, END, feed="iex", adjustment="raw") == {}
    assert t.calls == []


def test_bars_never_use_the_instance_marks_feed() -> None:
    t = FakeTransport()
    a = AlpacaPaperReadAdapter(t, clock=lambda: AFTER_CLOSE, feed="iex", sleep=lambda _d: None)
    t.respond("data", "/stocks/bars", js({"bars": {}, "next_page_token": None}))
    a.get_daily_bars(["AAPL"], START, END, feed="sip", adjustment="raw")
    assert t.calls[0][2]["feed"] == "sip"


# ============================================================ C8 path unchanged
def test_c8_daily_bar_closes_request_is_unchanged() -> None:
    a, t = adapter()
    t.respond("data", "/stocks/bars", js({"bars": {"AAPL": [
        {"t": "2026-11-09T05:00:00Z", "c": "151.1234"},  # close-only rows still accepted
        {"t": "2026-11-10T05:00:00Z", "c": "999"}]}, "next_page_token": None}))  # fmt: skip
    closes = a.get_daily_bar_closes(["AAPL"], date(2026, 11, 9))
    assert closes["AAPL"].close == D("151.1234") and closes["AAPL"].feed == "iex"
    assert t.calls[0][2] == {"symbols": "AAPL", "timeframe": "1Day", "start": "2026-11-09",
                             "end": "2026-11-09", "feed": "iex", "adjustment": "raw",
                             "limit": "1000"}  # fmt: skip


def test_official_closes_and_observe_baselines_are_unchanged() -> None:
    a, t = adapter()
    with pytest.raises(UnsupportedBrokerOperationError):
        a.get_official_closes(["AAPL"], date(2026, 11, 9))
    assert t.calls == []
    config = AppConfig.load(ROOT / "config")
    observe = config.loss_limits(RuntimeMode.OBSERVE)
    assert observe.baseline_close_sources == (CloseSource.OFFICIAL,)
    assert {c.value for c in CloseSource} == {"OFFICIAL", "DAILY_BAR", "SIM"}


def test_observe_commands_never_request_bars(tmp_path: Path) -> None:
    e: CliEnv = cli_env(tmp_path)
    e.ready()
    assert e.run("sync")[0] == 0
    assert e.run("status", "--broker")[0] == 0
    assert "/stocks/bars" not in e.account.paths_called()


# ============================================================ SimBroker
def test_sim_serves_scripted_bars_through_the_shared_mapper() -> None:
    s = sim()
    s.load_daily_bars("AAPL", history(ALL), feed="sip", adjustment="raw")
    s.load_daily_bars("MSFT", history(ALL, start_price="400"), feed="sip", adjustment="raw")
    out = s.get_daily_bars(["AAPL", "MSFT", "SPY"], ALL[2], ALL[5], feed="sip",
                           adjustment="raw")  # fmt: skip
    assert [b.session_date for b in out["AAPL"]] == ALL[2:6]
    assert out["MSFT"][0].open == D("400.20") and out["SPY"] == ()
    assert s.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw") == {"AAPL": ()}


def test_sim_split_adjusted_and_raw_series_are_distinct() -> None:
    raw, adjusted = split_pair(ALL)
    s = sim()
    s.load_daily_bars("ACME", raw, feed="sip", adjustment="raw")
    s.load_daily_bars("ACME", adjusted, feed="sip", adjustment="split")
    r = s.get_daily_bars(["ACME"], START, END, feed="sip", adjustment="raw")["ACME"]
    a = s.get_daily_bars(["ACME"], START, END, feed="sip", adjustment="split")["ACME"]
    before = ALL.index(SPLIT_DAY) - 1
    assert r[before].close == 2 * a[before].close and r[before].volume * 2 == a[before].volume
    assert r[-1].close == a[-1].close  # identical after the split
    assert {b.adjustment for b in r} == {"raw"} and {b.adjustment for b in a} == {"split"}


def test_sim_malformed_and_unordered_rows_fail_like_alpaca() -> None:
    s = sim()
    s.load_daily_bars("BAD", [{**bar_row(ALL[0], D(10)), "l": D(20)}])
    with pytest.raises(InvalidBrokerDataError, match="OHLC"):
        s.get_daily_bars(["BAD"], START, END, feed="iex", adjustment="raw")
    s.load_daily_bars("BAD", [bar_row(ALL[1], D(10)), bar_row(ALL[0], D(10))])
    with pytest.raises(InvalidBrokerDataError, match="out-of-order"):
        s.get_daily_bars(["BAD"], START, END, feed="iex", adjustment="raw")
    s.load_daily_bars("BAD", history(ALL[:2]))
    s.corrupt_next("get_daily_bars", lambda raw: {**raw, "v": 1.5})
    with pytest.raises(InvalidBrokerDataError, match="JSON integer"):
        s.get_daily_bars(["BAD"], START, END, feed="iex", adjustment="raw")
    assert len(s.get_daily_bars(["BAD"], START, END, feed="iex", adjustment="raw")["BAD"]) == 2


def test_sim_missing_optional_values() -> None:
    s = sim()
    s.load_daily_bars("AAPL", history(ALL[:2], optional=False))
    bars = s.get_daily_bars(["AAPL"], START, END, feed="iex", adjustment="raw")["AAPL"]
    assert {(b.vwap, b.trade_count) for b in bars} == {(None, None)}


# ============================================================ history validation
def assess(rows: list[dict[str, Any]], *, now: datetime = AFTER_CLOSE,
           requirement: HistoryRequirement = SIP_RAW, symbol: str = "AAPL",
           received: datetime | None = None) -> Any:  # fmt: skip
    s = sim(received or now)
    s.load_daily_bars(symbol, rows, feed=requirement.feed, adjustment=requirement.adjustment)
    bars = s.get_daily_bars([symbol], START, END, feed=requirement.feed,
                            adjustment=requirement.adjustment)[symbol]  # fmt: skip
    return assess_history(symbol, bars, calendar=calendar(now), now=now, requirement=requirement)


def test_window_is_the_last_completed_sessions() -> None:
    cal = calendar(AFTER_CLOSE)
    assert last_completed_session(cal, AFTER_CLOSE) == date(2026, 11, 30)
    assert last_completed_session(cal, IN_SESSION) == date(2026, 11, 27)
    window = required_window(cal, AFTER_CLOSE, 20)
    assert window == tuple(ALL[-20:]) and len(window) == 20
    assert date(2026, 11, 26) not in window  # Thanksgiving


def test_complete_history_is_eligible() -> None:
    r = assess(history(ALL))
    assert r.status is HistoryStatus.ELIGIBLE and r.findings == ()
    assert [b.session_date for b in r.bars] == list(r.window) == ALL[-20:]


def test_incomplete_current_session_is_excluded() -> None:
    rows = history(ALL)  # includes 2026-11-30, still in progress at IN_SESSION
    r = assess(rows, now=IN_SESSION)
    assert r.status is HistoryStatus.ELIGIBLE, r.findings
    assert r.window[-1] == date(2026, 11, 27) and r.excluded == (date(2026, 11, 30),)
    assert date(2026, 11, 30) not in {b.session_date for b in r.bars}


def test_early_close_session_counts_after_its_own_close() -> None:
    rows = history(sessions(START, date(2026, 11, 27)))
    after = assess(rows, now=AFTER_EARLY_CLOSE)
    assert after.status is HistoryStatus.ELIGIBLE and after.window[-1] == date(2026, 11, 27)
    before_close = datetime(2026, 11, 27, 12, 0, tzinfo=MARKET_TZ).astimezone(UTC)
    early = assess(rows, now=AFTER_EARLY_CLOSE, received=before_close)  # partial bar
    assert early.status is HistoryStatus.INSUFFICIENT_HISTORY
    assert date(2026, 11, 27) in early.excluded
    assert {f.issue for f in early.findings} == {HistoryIssue.LATEST_SESSION_MISSING}


def test_missing_sessions_make_the_symbol_ineligible_without_filling() -> None:
    gap = {date(2026, 11, 12), date(2026, 11, 13)}  # e.g. a trading suspension
    r = assess(history(d for d in ALL if d not in gap))
    assert r.status is HistoryStatus.INSUFFICIENT_HISTORY and r.bars == ()
    (finding,) = r.findings
    assert finding.issue is HistoryIssue.MISSING_SESSIONS
    assert finding.detail == "2026-11-12, 2026-11-13"


def test_recent_listing_is_insufficient_history() -> None:
    r = assess(history(ALL[-8:]))
    assert r.status is HistoryStatus.INSUFFICIENT_HISTORY
    assert [f.issue for f in r.findings] == [HistoryIssue.HISTORY_STARTS_IN_WINDOW]
    assert "8 of 20 sessions" in r.findings[0].detail


def test_latest_session_missing_and_no_bars() -> None:
    stale = assess(history(ALL[:-1]))
    assert [f.issue for f in stale.findings] == [HistoryIssue.LATEST_SESSION_MISSING]
    none = assess([])
    assert none.status is HistoryStatus.INSUFFICIENT_HISTORY
    assert [f.issue for f in none.findings] == [HistoryIssue.NO_BARS]


def test_lookback_longer_than_available_history() -> None:
    r = assess(history(ALL), requirement=HistoryRequirement(60, "sip", "raw"))
    assert r.status is HistoryStatus.INSUFFICIENT_HISTORY
    assert r.findings[0].issue is HistoryIssue.HISTORY_STARTS_IN_WINDOW


def judge(bars: list[DailyBar], requirement: HistoryRequirement = SIP_RAW) -> Any:
    return assess_history("AAPL", bars, calendar=calendar(AFTER_CLOSE), now=AFTER_CLOSE,
                          requirement=requirement)  # fmt: skip


def complete_bars() -> list[DailyBar]:
    s = sim()
    s.load_daily_bars("AAPL", history(ALL), feed="sip", adjustment="raw")
    return list(s.get_daily_bars(["AAPL"], START, END, feed="sip", adjustment="raw")["AAPL"])


@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (lambda bs: [_with(bs[0], symbol="MSFT"), *bs[1:]], HistoryIssue.WRONG_SYMBOL),
        (lambda bs: [*bs[:-1], _with(bs[-1], feed="iex")], HistoryIssue.WRONG_FEED),
        (lambda bs: [*bs[:-1], _with(bs[-1], adjustment="split")], HistoryIssue.WRONG_ADJUSTMENT),
        (lambda bs: [*bs[:-2], bs[-1], bs[-2]], HistoryIssue.NOT_CHRONOLOGICAL),
        (lambda bs: [*bs, bs[-1]], HistoryIssue.DUPLICATE_SESSION),
        (lambda bs: [*bs[:-1], _with(bs[-1], as_of=bs[-1].as_of + timedelta(hours=9, minutes=30))],
         HistoryIssue.TIMESTAMP_NOT_SESSION_START),
        (lambda bs: [*bs[:-1], _weekend(bs[-1]), bs[-1]], HistoryIssue.NOT_A_TRADING_SESSION),
    ],
)  # fmt: skip
def test_malformed_history_is_distinct_from_insufficient(mutate: Any, issue: HistoryIssue) -> None:
    r = judge(mutate(complete_bars()))
    assert r.status is HistoryStatus.MALFORMED, r.findings
    assert issue in {f.issue for f in r.findings} and r.bars == ()


def _with(bar: DailyBar, **changes: Any) -> DailyBar:
    return replace(bar, **changes)


def _weekend(bar: DailyBar) -> DailyBar:
    sat = date(2026, 11, 28)
    return _with(bar, session_date=sat, as_of=bar.as_of - timedelta(days=2))


def test_calendar_problems_are_reported_as_unavailable() -> None:
    stale_at = AFTER_CLOSE - timedelta(days=5)
    data = CalendarData().with_fetch(trading_fetch(stale_at, date(2026, 11, 1), date(2026, 11, 20)))
    cal = TradingCalendar(data, as_of=AFTER_CLOSE, max_age=timedelta(hours=36))
    r = assess_history("AAPL", complete_bars(), calendar=cal, now=AFTER_CLOSE,
                       requirement=SIP_RAW)  # fmt: skip
    assert r.status is HistoryStatus.CALENDAR_UNAVAILABLE and r.window == ()


def test_symbols_are_judged_independently() -> None:
    s = sim()
    s.load_daily_bars("GOOD", history(ALL), feed="sip", adjustment="raw")
    s.load_daily_bars("NEW", history(ALL[-3:]), feed="sip", adjustment="raw")
    bars = s.get_daily_bars(["GOOD", "NEW", "NONE"], START, END, feed="sip", adjustment="raw")
    out = assess_histories(bars, calendar=calendar(AFTER_CLOSE), now=AFTER_CLOSE,
                           requirement=SIP_RAW)  # fmt: skip
    assert {k: v.status for k, v in out.items()} == {
        "GOOD": HistoryStatus.ELIGIBLE,
        "NEW": HistoryStatus.INSUFFICIENT_HISTORY,
        "NONE": HistoryStatus.INSUFFICIENT_HISTORY,
    }


def test_split_adjusted_requirement_accepts_only_adjusted_bars() -> None:
    raw, adjusted = split_pair(ALL)
    split = HistoryRequirement(20, "sip", "split")
    assert assess(adjusted, requirement=split).status is HistoryStatus.ELIGIBLE
    assert assess(raw, requirement=SIP_RAW).status is HistoryStatus.ELIGIBLE
    mixed = judge(complete_bars(), requirement=split)  # raw bars, split required
    assert mixed.status is HistoryStatus.MALFORMED


def test_requirement_must_be_positive() -> None:
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="lookback_sessions"):
            HistoryRequirement(bad, "sip", "raw")  # type: ignore[arg-type]


# ============================================================ zero volume (16.4A)
def test_zero_volume_is_policy_ineligibility_not_malformed_data() -> None:
    bars = complete_bars()
    zero = [*bars[:-1], _with(bars[-1], volume=0)]
    r = judge(zero)
    assert r.status is HistoryStatus.INELIGIBLE and r.bars == ()
    assert [f.issue for f in r.findings] == [HistoryIssue.ZERO_VOLUME]
    lax = judge(zero, requirement=HistoryRequirement(20, "sip", "raw", require_volume=False))
    assert lax.status is HistoryStatus.ELIGIBLE and lax.bars[-1].volume == 0


def test_malformed_and_insufficient_take_precedence_over_policy() -> None:
    bars = complete_bars()
    n = len(bars)  # edit sessions inside the 20-session window
    gap_and_zero = [_with(b, volume=0) if i == n - 5 else b
                    for i, b in enumerate(bars) if i != n - 10]  # fmt: skip
    assert judge(gap_and_zero).status is HistoryStatus.INSUFFICIENT_HISTORY
    broken = [*gap_and_zero, gap_and_zero[-1]]
    assert judge(broken).status is HistoryStatus.MALFORMED
