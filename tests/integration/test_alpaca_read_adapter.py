"""Step 9: AlpacaPaperReadAdapter against a scripted offline transport (no network)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from swing_trading_bot.broker.activity_feed import collect_activities, merge_activities
from swing_trading_bot.broker.alpaca_http import ReadRetryPolicy
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter, iso_param
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerRequestError,
    InvalidBrokerDataError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UnknownBrokerStatusError,
)
from swing_trading_bot.broker.models import ActivityPage, BrokerActivity
from swing_trading_bot.broker.ports import BrokerExecutionPort, BrokerReadPort, PriceMarkSource
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarData, CalendarKind, TradingCalendar
from swing_trading_bot.domain.valuation import MarkFreshnessPolicy, MarkStatus, assess_mark
from tests.support.alpaca_fake import (
    ACCOUNT,
    CLOCK,
    CONFIG,
    T_NOW,
    FakeTransport,
    cash_activity,
    fill_activity,
    js,
    now_fn,
    order,
)
from tests.support.calendar_fixture import settlement_fetch, trading_fetch

D = Decimal


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[timedelta] = []

    def __call__(self, delay: timedelta) -> None:
        self.delays.append(delay)


def make(**kwargs: Any) -> tuple[AlpacaPaperReadAdapter, FakeTransport, Sleeps]:
    transport, sleeps = FakeTransport(), Sleeps()
    adapter = AlpacaPaperReadAdapter(transport, clock=now_fn(T_NOW), sleep=sleeps, **kwargs)
    return adapter, transport, sleeps


# ------------------------------------------------------------ structure


def test_implements_read_port_and_marks_only() -> None:
    adapter, _, _ = make()
    assert isinstance(adapter, BrokerReadPort)
    assert isinstance(adapter, PriceMarkSource)
    assert not isinstance(adapter, BrokerExecutionPort)
    for name in ("submit_order", "cancel_order", "replace_order", "replace_order_by_id",
                 "close_position", "cancel_orders"):  # fmt: skip
        assert not hasattr(adapter, name)
    assert "read-only" in repr(adapter)
    with pytest.raises(ValueError, match="feed"):
        make(feed="otc")


# -------------------------------------------------- account, config, clock


def test_account_exact_decimals_and_ignores_buying_power() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/account", js(ACCOUNT))
    a = adapter.get_account()
    assert a.cash == D("100000.12") and str(a.cash) == "100000.12"
    assert a.pending_reg_taf_fees == D("0.01") and a.multiplier == D(4)
    assert a.as_of == T_NOW
    assert not hasattr(a, "buying_power")
    assert t.calls == [("trading", "/account", {})]


@pytest.mark.parametrize(
    ("patch", "message"),
    [({"cash": 100000.12}, "must be a string"), ({"cash": "1e400x"}, "decimal"),
     ({"trading_blocked": "false"}, "boolean"), ({"status": None}, "missing")],
)  # fmt: skip
def test_account_rejects_malformed_values(patch: dict[str, Any], message: str) -> None:
    adapter, t, _ = make()
    t.respond("trading", "/account", {**ACCOUNT, **patch})  # dict: as if decoded
    with pytest.raises(InvalidBrokerDataError, match=message):
        adapter.get_account()


def test_account_configuration_and_clock() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/account/configurations", js(CONFIG))
    t.respond("trading", "/clock", js(CLOCK))
    cfg = adapter.get_account_config()
    assert cfg.no_shorting and cfg.max_margin_multiplier == 1 and cfg.m2_protections_in_place
    clock = adapter.get_clock()
    assert clock.timestamp == datetime(2026, 11, 9, 15, 0, 0, 123456, tzinfo=UTC)  # ns truncated
    assert clock.next_open.tzinfo is UTC and clock.is_open
    t.respond("trading", "/clock", js({**CLOCK, "timestamp": "2026-11-09T10:00:00"}))
    with pytest.raises(InvalidBrokerDataError, match="UTC offset"):
        adapter.get_clock()


# ------------------------------------------------------------- calendar


def _cal_rows() -> list[dict[str, str]]:
    return [
        {"date": "2026-11-25", "open": "09:30", "close": "16:00", "session_open": "0400",
         "session_close": "2000", "settlement_date": "2026-11-27"},
        {"date": "2026-11-27", "open": "09:30", "close": "13:00", "session_open": "0400",
         "session_close": "1700", "settlement_date": "2026-11-30"},
    ]  # fmt: skip


def test_trading_and_settlement_calendars() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/calendar", js(_cal_rows()))
    fetch = adapter.get_calendar(CalendarKind.TRADING, date(2026, 11, 23), date(2026, 11, 30))
    assert t.calls[-1][2] == {"start": "2026-11-23", "end": "2026-11-30", "date_type": "TRADING"}
    wed, fri = fetch.sessions
    assert wed.open_at == datetime(2026, 11, 25, 14, 30, tzinfo=UTC)  # 09:30 EST
    assert fri.is_early_close and fri.close_local.time() == time(13, 0)
    assert fri.settlement_date == date(2026, 11, 30)
    assert fetch.fetched_at == T_NOW and fetch.source == "alpaca"
    assert fetch.problems() == []
    t.respond("trading", "/calendar", js([{"date": "2026-11-24"}, {"date": "2026-11-25"}]))
    s = adapter.get_calendar(CalendarKind.SETTLEMENT, date(2026, 11, 24), date(2026, 11, 25))
    assert s.settlement_days == (date(2026, 11, 24), date(2026, 11, 25))
    assert t.calls[-1][2]["date_type"] == "SETTLEMENT"


@pytest.mark.parametrize(
    ("patch", "message"),
    [({"open": "9:30"}, "HH:MM"), ({"close": "25:00"}, "HH:MM"), ({"open": "09:30:00"}, "HH:MM"),
     ({"date": "2026/11/25"}, "YYYY-MM-DD"), ({"settlement_date": "Nov 27"}, "YYYY-MM-DD"),
     ({"open": None}, "missing"), ({"close": 1600}, "non-empty string")],
)  # fmt: skip
def test_invalid_calendar_fields(patch: dict[str, Any], message: str) -> None:
    adapter, t, _ = make()
    t.respond("trading", "/calendar", [{**_cal_rows()[0], **patch}])
    with pytest.raises(InvalidBrokerDataError, match=message):
        adapter.get_calendar(CalendarKind.TRADING, date(2026, 11, 23), date(2026, 11, 30))


def test_calendar_contradictions_surface_as_fetch_problems() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/calendar", js([{**_cal_rows()[0], "open": "16:00", "close": "09:30"}]))
    fetch = adapter.get_calendar(CalendarKind.TRADING, date(2026, 11, 23), date(2026, 11, 30))
    assert any("open is not before close" in p for p in fetch.problems())


# ------------------------------------------------------------ positions


def test_positions_and_current_price_is_crosscheck_only() -> None:
    adapter, t, _ = make()
    rows = [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "150.2500", "side": "long",
             "current_price": "151.10", "market_value": "1511.00"}]  # fmt: skip
    t.always("trading", "/positions", js(rows))
    (p,) = adapter.get_positions()
    assert (p.symbol, p.qty, p.avg_entry_price) == ("AAPL", D(10), D("150.2500"))
    assert not hasattr(p, "current_price")
    assert adapter.get_position_price_crosscheck() == {"AAPL": D("151.10")}
    t.respond("trading", "/positions", js(rows * 2))
    with pytest.raises(InvalidBrokerDataError, match="duplicate symbol"):
        adapter.get_positions()


# --------------------------------------------------------------- orders


def test_open_orders_and_orders_since() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/orders", js([order("stb-1"), order("stb-2", status="partially_filled",
                                                           filled="4")]))  # fmt: skip
    opened = adapter.get_open_orders()
    assert [o.client_order_id for o in opened] == ["stb-1", "stb-2"]
    assert opened[1].filled_qty == D(4)
    assert t.calls[-1][2] == {"status": "open", "direction": "asc", "nested": "false",
                              "limit": "500"}  # fmt: skip
    t.respond("trading", "/orders", js([order("stb-3", status="filled", filled="10",
                                              filled_avg_price="150.1234")]))  # fmt: skip
    since = adapter.get_orders(after=T_NOW - timedelta(days=1), until=T_NOW)
    assert since[0].filled_avg_price == D("150.1234")
    assert t.calls[-1][2]["after"] == "2026-11-08T15:05:00Z"
    assert t.calls[-1][2]["until"] == "2026-11-09T15:05:00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        adapter.get_orders(after=datetime(2026, 11, 9))  # noqa: DTZ001


def test_orders_pagination_with_boundary_overlap() -> None:
    adapter, t, _ = make()
    first = [order(f"stb-{i}", submitted=f"2026-11-09T15:00:{i // 10:02d}.000000Z")
             for i in range(500)]  # fmt: skip
    second = [first[-1], order("stb-500", submitted="2026-11-09T15:01:00.000000Z")]
    t.respond("trading", "/orders", js(first), js(second))
    out = adapter.get_orders(after=T_NOW - timedelta(hours=1))
    assert len(out) == 501 and len({o.broker_order_id for o in out}) == 501
    # last order of page 1 was submitted at 15:00:49 -> next page starts 1 µs earlier
    assert t.calls[-1][2]["after"] == "2026-11-09T15:00:48.999999Z"
    t.respond("trading", "/orders", js(first), js(first))
    with pytest.raises(InvalidBrokerDataError, match="no progress"):
        adapter.get_orders(after=T_NOW - timedelta(hours=1))


@pytest.mark.parametrize(
    ("patch", "error", "message"),
    [({"status": "held_for_review"}, UnknownBrokerStatusError, "unknown order status"),
     ({"status": "NEW"}, UnknownBrokerStatusError, "unknown order status"),
     ({"updated_at": "2026-11-09 15:00:00"}, InvalidBrokerDataError, "UTC offset"),
     ({"updated_at": "not-a-time"}, InvalidBrokerDataError, "ISO-8601"),
     ({"qty": 10}, InvalidBrokerDataError, "must be a string"),
     ({"time_in_force": "opg"}, InvalidBrokerDataError, "unknown value")],
)  # fmt: skip
def test_malformed_orders_rejected(patch: dict[str, Any], error: type[Exception],
                                   message: str) -> None:  # fmt: skip
    adapter, t, _ = make()
    t.respond("trading", "/orders", [order("stb-1", **patch)])
    with pytest.raises(error, match=message):
        adapter.get_open_orders()


def test_order_lookup_found_404_and_identity_check() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/orders:by_client_order_id", js(order("stb-9")),
              OrderNotFoundError("404", status_code=404), js(order("stb-other")))  # fmt: skip
    assert adapter.get_order_by_client_id("stb-9") is not None
    assert t.calls[-1][2] == {"client_order_id": "stb-9"}
    assert adapter.get_order_by_client_id("stb-9") is None  # 404 -> None (not proof)
    with pytest.raises(InvalidBrokerDataError, match="different client_order_id"):
        adapter.get_order_by_client_id("stb-9")
    with pytest.raises(ValueError, match="required"):
        adapter.get_order_by_client_id("")


# ----------------------------------------------------------- activities


def test_activity_kinds_and_exact_values() -> None:
    adapter, t, _ = make()
    rows = [
        fill_activity("opaque-Z9"),
        cash_activity("opaque-A1", "FEE", "-0.02", symbol="AAPL"),
        cash_activity("opaque-M5", "DIV", "1.20", symbol="AAPL"),
        cash_activity("opaque-B7", "JNLC", "25.00"),  # journal / adjustment
    ]
    t.respond("trading", "/account/activities", js(rows))
    page = adapter.get_activities(page_token="opaque-cursor", page_size=50,
                                  after=T_NOW - timedelta(days=2), until=T_NOW)  # fmt: skip
    assert t.calls[-1][2] == {
        "direction": "asc",
        "page_size": "50",
        "page_token": "opaque-cursor",
        "after": "2026-11-07T15:05:00Z",
        "until": "2026-11-09T15:05:00Z",
    }
    f, fee, div, jnl = page.activities
    assert (f.price, f.qty, f.order_id) == (D("150.2501"), D(4), "oid-1")
    assert f.occurred_at == datetime(2026, 11, 9, 15, 1, 0, 574000, tzinfo=UTC)
    assert (fee.net_amount, div.net_amount, jnl.net_amount) == (D("-0.02"), D("1.20"), D("25.00"))
    assert jnl.activity_type == "JNLC" and jnl.occurred_at is None
    assert [a.activity_id for a in page.activities] == ["opaque-Z9", "opaque-A1", "opaque-M5",
                                                       "opaque-B7"]  # fmt: skip


@pytest.mark.parametrize(
    ("rows", "message"),
    [([fill_activity("a"), fill_activity("a")], "duplicate id"),
     ([fill_activity("a", qty=4)], "must be a string"),  # type: ignore[arg-type]
     ([{**fill_activity("a"), "id": None}], "missing"),
     ([{k: v for k, v in cash_activity("b", "FEE", "-1").items() if k != "net_amount"}],
      "net_amount"),
     ([fill_activity("a", t="2026-11-09T15:01:00")], "UTC offset"),
     ([fill_activity("cursor-1")], "repeats the cursor")],
)  # fmt: skip
def test_malformed_or_duplicated_activity_records(rows: list[dict[str, Any]],
                                                  message: str) -> None:  # fmt: skip
    adapter, t, _ = make()
    t.respond("trading", "/account/activities", rows)
    with pytest.raises(InvalidBrokerDataError, match=message):
        adapter.get_activities(page_token="cursor-1")


def test_page_larger_than_requested_rejected() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/account/activities", js([fill_activity(f"x{i}") for i in range(3)]))
    with pytest.raises(InvalidBrokerDataError, match="larger than requested"):
        adapter.get_activities(page_token=None, page_size=2)
    with pytest.raises(ValueError, match="page_size"):
        adapter.get_activities(page_token=None, page_size=101)


class FeedServer:
    """Fake activities endpoint: opaque, non-sortable ids; cursor = position of the id;
    activities can be posted late (with an earlier transaction time)."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def post(self, aid: str, at: datetime) -> None:
        self.items.append(fill_activity(aid, t=at.isoformat().replace("+00:00", "Z")))

    def __call__(self, params: Mapping[str, str]) -> list[dict[str, Any]]:
        def ts(item: dict[str, Any]) -> datetime:
            return datetime.fromisoformat(str(item["transaction_time"]).replace("Z", "+00:00"))

        after = params.get("after")
        until = params.get("until")

        def parse(value: str) -> datetime:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        rows = [
            i
            for i in self.items
            if (after is None or ts(i) > parse(after)) and (until is None or ts(i) <= parse(until))
        ]
        token = params.get("page_token")
        if token is not None:
            ids = [r["id"] for r in rows]
            rows = rows[ids.index(token) + 1 :] if token in ids else []
        return rows[: int(params["page_size"])]


def test_pagination_overlapping_windows_and_late_posted_activity() -> None:
    adapter, t, _ = make()
    server = FeedServer()
    t.always("trading", "/account/activities", server)
    base = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    ids = ["k7", "b2", "zz", "a0", "m1", "q9", "c3"]  # deliberately unordered, opaque
    for i, aid in enumerate(ids):
        server.post(aid, base + timedelta(minutes=i))
    window1 = collect_activities(adapter, after=base - timedelta(minutes=1),
                                 until=base + timedelta(minutes=4), page_size=2)  # fmt: skip
    assert [a.activity_id for a in window1] == ["k7", "b2", "zz", "a0", "m1"]
    server.post("late-x", base + timedelta(minutes=2, seconds=30))  # posted late, earlier time
    window2 = collect_activities(adapter, after=base + timedelta(minutes=2),
                                 until=base + timedelta(minutes=10), page_size=2)  # fmt: skip
    merged = merge_activities(window1, window2)
    assert [a.activity_id for a in merged] == ["k7", "b2", "zz", "a0", "m1", "q9", "c3", "late-x"]
    assert len(merged) == len({a.activity_id for a in merged})  # overlap deduplicated


def test_merge_rejects_conflicting_duplicates_and_stuck_cursor() -> None:
    a = fill_activity("same")
    adapter, t, _ = make()
    t.respond("trading", "/account/activities", js([a]), js([{**a, "price": "1.00"}]))
    first = adapter.get_activities(page_token=None).activities
    second = adapter.get_activities(page_token=None).activities
    with pytest.raises(InvalidBrokerDataError, match="different content"):
        merge_activities(first, second)

    class Stuck:
        def get_activities(self, **_: Any) -> ActivityPage:
            act = BrokerActivity("s1", "FEE", date(2026, 11, 9), None, "{}", net_amount=D(-1))
            return ActivityPage((act,), page_size=1)

    with pytest.raises(InvalidBrokerDataError, match="no progress"):
        collect_activities(Stuck(), after=None, until=None, page_size=1)  # type: ignore[arg-type]


# ----------------------------------------------- errors, retries, outages


def test_authentication_failure_is_not_retried() -> None:
    adapter, t, sleeps = make()
    t.respond("trading", "/account", AuthenticationError("401", status_code=401))
    with pytest.raises(AuthenticationError):
        adapter.get_account()
    assert len(t.calls) == 1 and sleeps.delays == []


def test_rate_limit_bounded_retries_honour_retry_after() -> None:
    adapter, t, sleeps = make()
    t.respond("trading", "/positions",
              RateLimitedError("429", retry_after=timedelta(seconds=3)),
              RateLimitedError("429", retry_after=timedelta(seconds=30)), js([]))  # fmt: skip
    assert adapter.get_positions() == []
    assert len(t.calls) == 3
    assert sleeps.delays == [timedelta(seconds=3), timedelta(seconds=4)]  # capped at max_delay


def test_retries_are_bounded_per_operation() -> None:
    adapter, t, sleeps = make()
    t.always("trading", "/account", ServiceUnavailableError("503", status_code=503))
    with pytest.raises(ServiceUnavailableError):
        adapter.get_account()
    assert len(t.calls) == 3  # standard policy: 3 attempts
    t.calls.clear()
    t.always("trading", "/orders:by_client_order_id", ServiceUnavailableError("timeout"))
    with pytest.raises(ServiceUnavailableError):
        adapter.get_order_by_client_id("stb-1")
    assert len(t.calls) == 4  # recovery lookups get one more attempt
    t.calls.clear()
    t.always("data", "/stocks/trades/latest", RateLimitedError("429"))
    with pytest.raises(RateLimitedError):
        adapter.get_marks(["AAPL"])
    assert len(t.calls) == 2  # marks: stale beats slow
    assert all(d <= timedelta(seconds=4) for d in sleeps.delays)


def test_request_errors_are_not_retried() -> None:
    adapter, t, _ = make()
    t.respond("trading", "/clock", BrokerRequestError("400", status_code=400))
    with pytest.raises(BrokerRequestError):
        adapter.get_clock()
    assert len(t.calls) == 1


def test_custom_policy_injection() -> None:
    adapter, t, _ = make(policies={"clock": ReadRetryPolicy(1, timedelta(0), timedelta(0))})
    t.always("trading", "/clock", ServiceUnavailableError("down"))
    with pytest.raises(ServiceUnavailableError):
        adapter.get_clock()
    assert len(t.calls) == 1


# ------------------------------------------------------- market prices


def _calendar(as_of: datetime) -> TradingCalendar:
    data = CalendarData().with_fetch(trading_fetch(as_of)).with_fetch(settlement_fetch(as_of))
    return TradingCalendar(data, as_of=as_of, max_age=timedelta(hours=36))


POLICY = MarkFreshnessPolicy.from_seconds(in_session=900, future_skew=5, receipt_age=120)


def test_latest_trade_marks_are_exact_and_timestamped() -> None:
    adapter, t, _ = make()
    body = ('{"trades": {"AAPL": {"t": "2026-11-09T15:04:30.334320128Z", "x": "V", '
            '"p": 150.2501, "s": 100, "c": ["@"], "i": 1, "z": "C"}}}')  # fmt: skip
    t.respond("data", "/stocks/trades/latest", body)
    marks = adapter.get_marks(["AAPL", "MSFT", "AAPL"])
    assert t.calls[-1] == ("data", "/stocks/trades/latest", {"symbols": "AAPL,MSFT", "feed": "iex"})
    assert list(marks) == ["AAPL"]  # MSFT missing: absent, never defaulted
    m = marks["AAPL"]
    assert m.price == D("150.2501") and str(m.price) == "150.2501"
    assert m.as_of == datetime(2026, 11, 9, 15, 4, 30, 334320, tzinfo=UTC)
    assert (m.received_at, m.source.value, m.feed) == (T_NOW, "LATEST_TRADE", "iex")
    assert assess_mark(m, now=T_NOW, calendar=_calendar(T_NOW), policy=POLICY).fresh
    assert assess_mark(None, now=T_NOW, calendar=_calendar(T_NOW),
                       policy=POLICY).status is MarkStatus.MISSING  # fmt: skip


def test_stale_and_invalid_market_prices() -> None:
    adapter, t, _ = make()
    t.respond("data", "/stocks/trades/latest",
              js({"trades": {"AAPL": {"t": "2026-11-06T20:59:59Z", "p": "149.00"}}}))  # fmt: skip
    old = adapter.get_marks(["AAPL"])["AAPL"]
    assessed = assess_mark(old, now=T_NOW, calendar=_calendar(T_NOW), policy=POLICY)
    assert assessed.status is MarkStatus.STALE  # previous session's print during RTH
    for body, message in [
        ({"trades": {"AAPL": {"t": "2026-11-09T15:04:00Z", "p": 150.25}}}, "exact number"),
        ({"trades": {"AAPL": {"t": "2026-11-09T15:04:00Z", "p": "0"}}}, "non-positive"),
        ({"trades": {"TSLA": {"t": "2026-11-09T15:04:00Z", "p": "1"}}}, "unrequested"),
        ({"trades": {"AAPL": {"p": "1"}}}, "missing"),
        ({"bars": {}}, "expected a JSON object"),
    ]:
        t.respond("data", "/stocks/trades/latest", body)
        with pytest.raises(InvalidBrokerDataError, match=message):
            adapter.get_marks(["AAPL"])
    assert adapter.get_marks([]) == {}
    with pytest.raises(ValueError, match="invalid symbol"):
        adapter.get_marks(["aapl"])


def test_session_closes_from_daily_bars_with_pagination() -> None:
    adapter, t, _ = make(feed="sip")
    page1 = ('{"bars": {"AAPL": [{"t": "2026-11-09T05:00:00Z", "o": 150, "h": 152.5, '
             '"l": 149.75, "c": 151.1234, "v": 1000}]}, "next_page_token": "tok-1"}')  # fmt: skip
    page2 = js({"bars": {"MSFT": [{"t": "2026-11-09T05:00:00Z", "c": "410.00"},
                                  {"t": "2026-11-10T05:00:00Z", "c": "999"}]},
                "next_page_token": None})  # fmt: skip
    t.respond("data", "/stocks/bars", page1, page2)
    closes = adapter.get_session_closes(["AAPL", "MSFT", "SPY"], date(2026, 11, 9))
    assert {k: v.close for k, v in closes.items()} == {"AAPL": D("151.1234"), "MSFT": D("410.00")}
    assert closes["AAPL"].as_of.astimezone(MARKET_TZ).date() == date(2026, 11, 9)
    assert closes["AAPL"].feed == "sip"
    assert t.calls[-1][2]["page_token"] == "tok-1"
    assert t.calls[0][2]["adjustment"] == "raw" and t.calls[0][2]["timeframe"] == "1Day"
    t.respond("data", "/stocks/bars", js({"bars": {"AAPL": [
        {"t": "2026-11-09T05:00:00Z", "c": "1"}, {"t": "2026-11-09T05:00:00Z", "c": "2"}]},
        "next_page_token": None}))  # fmt: skip
    with pytest.raises(InvalidBrokerDataError, match="two daily bars"):
        adapter.get_session_closes(["AAPL"], date(2026, 11, 9))


def test_iso_param() -> None:
    assert iso_param(datetime(2026, 11, 9, 10, tzinfo=MARKET_TZ)) == "2026-11-09T15:00:00Z"


class ScriptedPort:
    """Minimal read port returning scripted activity pages (for collector edge cases)."""

    def __init__(self, *pages: tuple[BrokerActivity, ...], page_size: int = 2) -> None:
        self.pages = list(pages)
        self.page_size = page_size
        self.tokens: list[str | None] = []

    def get_activities(self, *, page_token: str | None, **_: Any) -> ActivityPage:
        self.tokens.append(page_token)
        return ActivityPage(self.pages.pop(0) if self.pages else (), page_size=self.page_size)


def _act(aid: str, amount: str = "-1") -> BrokerActivity:
    return BrokerActivity(aid, "FEE", date(2026, 11, 9), None, f'{{"id":"{aid}","a":"{amount}"}}',
                          net_amount=D(amount))  # fmt: skip


def test_collector_edge_cases() -> None:
    port = ScriptedPort((_act("p"), _act("q")), (_act("r"),))
    got = collect_activities(port, after=None, until=None, page_size=2)  # type: ignore[arg-type]
    assert [a.activity_id for a in got] == ["p", "q", "r"]
    assert port.tokens == [None, "q"]  # cursor passed back verbatim
    with pytest.raises(ValueError, match="max_pages"):
        collect_activities(port, after=None, until=None, max_pages=0)  # type: ignore[arg-type]
    endless = ScriptedPort(*[(_act(f"a{i}"), _act(f"b{i}")) for i in range(5)])
    with pytest.raises(InvalidBrokerDataError, match="exceeded 3 pages"):
        collect_activities(endless, after=None, until=None, max_pages=3)  # type: ignore[arg-type]
    stuck_cursor = ScriptedPort((_act("x"), _act("y")), (_act("z"), _act("y")))
    with pytest.raises(InvalidBrokerDataError, match="did not advance"):
        collect_activities(stuck_cursor, after=None, until=None)  # type: ignore[arg-type]
    conflicting = ScriptedPort((_act("d", "-1"), _act("d", "-2")))
    with pytest.raises(InvalidBrokerDataError, match="different content"):
        collect_activities(conflicting, after=None, until=None)  # type: ignore[arg-type]
