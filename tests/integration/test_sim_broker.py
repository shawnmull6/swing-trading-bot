"""Step 8: deterministic SimBroker behaviour through the broker ports."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    AmbiguousSubmissionError,
    AuthenticationError,
    DuplicateClientOrderIdError,
    InvalidBrokerDataError,
    NotCancelableError,
    OrderRejectedError,
    OrderState,
    RateLimitedError,
    ServiceUnavailableError,
    submit_failure_state,
)
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.ports import BrokerExecutionPort, BrokerReadPort, PriceMarkSource
from swing_trading_bot.broker.sim import (
    Accept,
    AcceptThenFail,
    AuthFailure,
    FailBeforeReceipt,
    RateLimit,
    Reject,
    SimBroker,
    SimClock,
    SimConfig,
    SimScriptError,
)
from swing_trading_bot.domain.calendar import CalendarKind
from swing_trading_bot.domain.enums import OrderType, Side, TimeInForce
from swing_trading_bot.domain.ids import new_client_order_id
from tests.support.calendar_fixture import settlement_fetch, trading_fetch
from tests.support.loss_limits import et

D = Decimal
MON = date(2026, 11, 9)
T0 = et(MON, 10)
MIN = timedelta(minutes=1)


def make_sim(**config: Any) -> tuple[SimBroker, SimClock]:
    clock = SimClock(T0)
    sim = SimBroker(clock, SimConfig(**config))
    sim.load_calendar(trading_fetch(T0))
    sim.load_calendar(settlement_fetch(T0))
    return sim, clock


def buy(qty: str = "10", limit: str = "100.00", symbol: str = "AAPL",
        coid: str | None = None) -> SubmitOrderRequest:  # fmt: skip
    return SubmitOrderRequest(
        client_order_id=coid or new_client_order_id(), symbol=symbol, side=Side.BUY,
        qty=D(qty), order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY,
        limit_price=D(limit),
    )  # fmt: skip


def sell(qty: str, limit: str = "100.00", symbol: str = "AAPL") -> SubmitOrderRequest:
    return SubmitOrderRequest(
        client_order_id=new_client_order_id(), symbol=symbol, side=Side.SELL, qty=D(qty),
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=D(limit),
    )  # fmt: skip


def test_implements_both_ports() -> None:
    sim, _ = make_sim()
    assert isinstance(sim, BrokerReadPort)
    assert isinstance(sim, BrokerExecutionPort)
    assert isinstance(sim, PriceMarkSource)


# ------------------------------------------------------------ order lifecycle


def test_successful_order_and_fill() -> None:
    sim, clock = make_sim()
    req = buy()
    ack = sim.submit_order(req)
    assert (ack.broker_status, ack.client_order_id, ack.filled_qty) == (
        "new",
        req.client_order_id,
        D(0),
    )
    assert sim.get_order_by_client_id(req.client_order_id) == ack
    assert [o.client_order_id for o in sim.get_open_orders()] == [req.client_order_id]
    sim.schedule_fill(req.client_order_id, D(10), D("99.95"), after=MIN)
    clock.advance(MIN)
    done = sim.get_order_by_client_id(req.client_order_id)
    assert done is not None
    assert (done.broker_status, done.filled_qty, done.filled_avg_price) == (
        "filled",
        D(10),
        D("99.95"),
    )
    assert sim.get_open_orders() == []
    (pos,) = sim.get_positions()
    assert (pos.symbol, pos.qty, pos.avg_entry_price) == ("AAPL", D(10), D("99.9500"))
    assert sim.get_account().cash == D("100000") - D("999.50")
    (act,) = sim.get_activities(page_token=None).activities
    assert act.is_fill and act.order_id == ack.broker_order_id
    assert (act.qty, act.price, act.cum_qty, act.leaves_qty) == (D(10), D("99.95"), D(10), D(0))
    assert (act.fill_type, act.order_status) == ("fill", "filled")
    assert act.occurred_at == clock.now() and act.activity_date == MON


def test_scripted_rejection_creates_no_order() -> None:
    sim, _ = make_sim()
    req = buy()
    sim.script_submit(Reject("symbol not tradable"), client_order_id=req.client_order_id)
    with pytest.raises(OrderRejectedError) as info:
        sim.submit_order(req)
    assert info.value.order_state is OrderState.NOT_CREATED and not info.value.read_retryable
    assert info.value.status_code == 422
    assert sim.get_order_by_client_id(req.client_order_id) is None
    assert sim.get_orders(after=T0 - MIN) == []


def test_business_rule_rejections() -> None:
    sim, _ = make_sim()
    with pytest.raises(OrderRejectedError, match="insufficient qty"):
        sim.submit_order(sell("1"))  # no position, shorting disabled
    with pytest.raises(OrderRejectedError, match="buying power"):
        sim.submit_order(buy(qty="5000", limit="100.00"))  # 500k > 400k sim power


def test_partial_fills() -> None:
    sim, clock = make_sim()
    req = buy()
    sim.submit_order(req)
    sim.schedule_fill(req.client_order_id, D(4), D("100.00"), after=MIN)
    sim.schedule_fill(req.client_order_id, D(6), D("99.00"), after=2 * MIN)
    clock.advance(MIN)
    o = sim.get_order_by_client_id(req.client_order_id)
    assert o is not None and (o.broker_status, o.filled_qty) == ("partially_filled", D(4))
    clock.advance(MIN)
    o = sim.get_order_by_client_id(req.client_order_id)
    assert o is not None and o.broker_status == "filled"
    assert o.filled_avg_price == D("99.4000")
    acts = sim.get_activities(page_token=None).activities
    assert [(a.fill_type, a.cum_qty, a.leaves_qty) for a in acts] == [
        ("partial_fill", D(4), D(6)),
        ("fill", D(10), D(0)),
    ]


def test_partial_fill_then_cancel() -> None:
    sim, clock = make_sim(cancel_delay=2 * MIN)
    req = buy()
    ack = sim.submit_order(req)
    sim.schedule_fill(req.client_order_id, D(4), D("100.00"), after=MIN)
    clock.advance(MIN)
    sim.cancel_order(ack.broker_order_id)
    o = sim.get_order_by_client_id(req.client_order_id)
    assert o is not None and o.broker_status == "pending_cancel"
    sim.schedule_fill(req.client_order_id, D(1), D("100.00"), after=MIN)  # races the cancel
    sim.schedule_fill(req.client_order_id, D(1), D("100.00"), after=3 * MIN)  # after it
    clock.advance(3 * MIN)
    o = sim.get_order_by_client_id(req.client_order_id)
    assert o is not None and (o.broker_status, o.filled_qty) == ("canceled", D(5))
    with pytest.raises(NotCancelableError):
        sim.cancel_order(ack.broker_order_id)
    assert any("ignored (canceled)" in line for line in sim.event_log)


def test_expiration_and_reject_after_accept() -> None:
    sim, clock = make_sim()
    a, b = buy(), buy(symbol="MSFT")
    sim.submit_order(a)
    sim.submit_order(b)
    sim.schedule_expire(a.client_order_id, after=MIN)
    sim.schedule_reject(b.client_order_id, after=MIN)
    clock.advance(MIN)
    statuses = {o.client_order_id: o.broker_status for o in sim.get_orders(after=T0 - MIN)}
    assert statuses == {a.client_order_id: "expired", b.client_order_id: "rejected"}


def test_script_errors_are_explicit() -> None:
    sim, _ = make_sim()
    req = buy(limit="100.00")
    sim.submit_order(req)
    with pytest.raises(SimScriptError, match="violates limit"):
        sim.schedule_fill(req.client_order_id, D(1), D("100.01"))
    with pytest.raises(SimScriptError, match="no order"):
        sim.schedule_fill("stb-unknown", D(1), D(1))
    with pytest.raises(SimScriptError, match="past"):
        sim.schedule_expire(req.client_order_id, after=-MIN)


# ------------------------------------------------------- duplicates & ambiguity


def test_duplicate_client_order_id() -> None:
    sim, _ = make_sim()
    req = buy()
    first = sim.submit_order(req)
    with pytest.raises(DuplicateClientOrderIdError) as info:
        sim.submit_order(req)
    assert info.value.order_state is OrderState.EXISTS
    assert sim.get_orders(after=T0 - MIN) == [first]


def test_accepted_but_gateway_timeout() -> None:
    sim, clock = make_sim()
    req = buy()
    sim.script_submit(AcceptThenFail("gateway_timeout"), client_order_id=req.client_order_id)
    with pytest.raises(AmbiguousSubmissionError) as info:
        sim.submit_order(req)
    assert info.value.status_code == 504
    assert info.value.order_state is OrderState.UNKNOWN
    found = sim.get_order_by_client_id(req.client_order_id)
    assert found is not None and found.broker_status == "new"
    with pytest.raises(DuplicateClientOrderIdError):  # a resend cannot create a second order
        sim.submit_order(req)
    assert len(sim.get_orders(after=T0 - MIN)) == 1
    sim.schedule_fill(req.client_order_id, D(10), D(100), after=MIN)
    clock.advance(MIN)
    assert len(sim.get_activities(page_token=None).activities) == 1


@pytest.mark.parametrize("failure", ["read_timeout", "connection_reset"])
def test_accepted_then_connection_failure(failure: str) -> None:
    sim, _ = make_sim()
    req = buy()
    sim.script_submit(AcceptThenFail(failure))  # type: ignore[arg-type]
    with pytest.raises(AmbiguousSubmissionError) as info:
        sim.submit_order(req)
    assert info.value.status_code is None
    assert sim.get_order_by_client_id(req.client_order_id) is not None


def test_lost_before_receipt_is_equally_ambiguous_but_never_created() -> None:
    sim, _ = make_sim()
    req = buy()
    sim.script_submit(FailBeforeReceipt(), client_order_id=req.client_order_id)
    with pytest.raises(AmbiguousSubmissionError):
        sim.submit_order(req)
    assert sim.get_order_by_client_id(req.client_order_id) is None
    assert sim.get_orders(after=T0 - MIN) == []
    assert sim.submit_order(req).client_order_id == req.client_order_id  # same id, one order
    assert len(sim.get_orders(after=T0 - MIN)) == 1


def test_rate_limit_and_auth_failure_on_submit() -> None:
    sim, _ = make_sim()
    sim.script_submit(RateLimit(retry_after=timedelta(seconds=2.5)))
    sim.script_submit(AuthFailure())
    req = buy()
    with pytest.raises(AmbiguousSubmissionError) as rl:  # 429 on a write proves nothing
        sim.submit_order(req)
    assert rl.value.status_code == 429
    assert submit_failure_state(rl.value) is OrderState.UNKNOWN
    with pytest.raises(AuthenticationError) as auth:
        sim.submit_order(req)
    assert not auth.value.read_retryable and auth.value.status_code == 401
    assert submit_failure_state(auth.value) is OrderState.UNKNOWN
    assert sim.submit_order(req).client_order_id == req.client_order_id


# ---------------------------------------------------------------- visibility


def test_delayed_order_visibility() -> None:
    sim, clock = make_sim(order_visibility_delay=8 * MIN)
    req = buy()
    sim.script_submit(AcceptThenFail("gateway_timeout"))
    with pytest.raises(AmbiguousSubmissionError):
        sim.submit_order(req)
    for _ in range(7):
        assert sim.get_order_by_client_id(req.client_order_id) is None
        assert sim.get_open_orders() == []
        clock.advance(MIN)
    assert sim.get_order_by_client_id(req.client_order_id) is None  # 7 min: still invisible
    clock.advance(MIN)
    assert sim.get_order_by_client_id(req.client_order_id) is not None


def test_per_order_visibility_override() -> None:
    sim, clock = make_sim()
    req = buy()
    sim.script_submit(Accept(visibility_delay=2 * MIN), client_order_id=req.client_order_id)
    sim.submit_order(req)
    assert sim.get_order_by_client_id(req.client_order_id) is None
    clock.advance(2 * MIN)
    assert sim.get_order_by_client_id(req.client_order_id) is not None


def test_delayed_activity_is_missed_by_cursor_and_found_by_overlap_rescan() -> None:
    sim, clock = make_sim()
    a, b = buy(symbol="AAPL"), buy(symbol="MSFT")
    sim.submit_order(a)
    sim.submit_order(b)
    sim.schedule_fill(a.client_order_id, D(10), D(100), after=MIN, activity_delay=10 * MIN)
    sim.schedule_fill(b.client_order_id, D(10), D(100), after=2 * MIN)
    clock.advance(3 * MIN)
    first = sim.get_activities(page_token=None)
    assert [x.symbol for x in first.activities] == ["MSFT"]
    cursor = first.last_id
    clock.advance(10 * MIN)
    assert sim.get_activities(page_token=cursor).activities == ()  # A sorts before cursor
    rescan = sim.get_activities(page_token=None, after=T0)
    assert [x.symbol for x in rescan.activities] == ["AAPL", "MSFT"]
    assert rescan.activities[0].activity_id != cursor  # ids are opaque: never compared


# ---------------------------------------------------------------- pagination


def test_activity_pagination() -> None:
    sim, clock = make_sim()
    req = buy(qty="250", limit="10.00")
    sim.submit_order(req)
    for i in range(250):
        sim.schedule_fill(req.client_order_id, D(1), D("10.00"), after=timedelta(seconds=i + 1))
    clock.advance(timedelta(seconds=300))
    seen: list[str] = []
    token: str | None = None
    sizes = []
    while True:
        page = sim.get_activities(page_token=token, page_size=100)
        sizes.append(len(page.activities))
        seen.extend(x.activity_id for x in page.activities)
        if not page.possibly_more:
            break
        token = page.last_id
    assert sizes == [100, 100, 50]
    assert seen == sorted(seen) and len(set(seen)) == 250
    assert sim.get_activities(page_token=seen[-1]).activities == ()  # empty tail
    exact = sim.get_activities(page_token=seen[149], page_size=100)
    assert exact.possibly_more and len(exact.activities) == 100  # boundary: ask once more
    assert sim.get_activities(page_token=exact.last_id).activities == ()
    for bad in (0, 101):
        with pytest.raises(ValueError, match="page_size"):
            sim.get_activities(page_token=None, page_size=bad)


def test_cash_activities_and_positions() -> None:
    sim, clock = make_sim()
    b = buy()
    sim.submit_order(b)
    sim.schedule_fill(b.client_order_id, D(10), D(100), after=MIN)
    clock.advance(MIN)
    s = sell("4", limit="110.00")
    sim.submit_order(s)
    sim.schedule_fill(s.client_order_id, D(4), D(110), after=MIN)
    sim.schedule_cash_activity("FEE", D("-0.02"), after=2 * MIN)
    sim.schedule_cash_activity("DIV", D("1.20"), symbol="AAPL", after=2 * MIN)
    clock.advance(2 * MIN)
    (pos,) = sim.get_positions()
    assert (pos.qty, pos.avg_entry_price) == (D(6), D("100.0000"))
    acts = sim.get_activities(page_token=None).activities
    assert [x.activity_type for x in acts] == ["FILL", "FILL", "FEE", "DIV"]
    fee, div = acts[2], acts[3]
    assert (fee.net_amount, fee.symbol, div.net_amount, div.symbol) == (
        D("-0.02"), None, D("1.20"), "AAPL")  # fmt: skip
    assert sim.get_account().cash == D("100000") - 1000 + 440 - D("0.02") + D("1.20")


# --------------------------------------------- account, config, clock, marks


def test_account_config_clock_calendar_and_marks() -> None:
    sim, clock = make_sim()
    cfg = sim.get_account_config()
    assert cfg.no_shorting and cfg.max_margin_multiplier == 1 and cfg.m2_protections_in_place
    acct = sim.get_account()
    assert acct.as_of == T0 and acct.multiplier == 4 and not acct.trading_blocked
    assert not hasattr(acct, "buying_power")  # never surfaced
    c = sim.get_clock()
    assert c.is_open and c.next_close == et(MON, 16)
    clock.advance_to(et(MON, 17))
    c = sim.get_clock()
    assert not c.is_open and c.next_open == et(date(2026, 11, 10), 9, 30)
    cal = sim.get_calendar(CalendarKind.TRADING, MON, date(2026, 11, 13))
    assert [s.session_date.day for s in cal.sessions] == [9, 10, 11, 12, 13]
    assert cal.fetched_at == clock.now() and cal.problems() == []
    with pytest.raises(ServiceUnavailableError):
        sim.get_calendar(CalendarKind.TRADING, MON, date(2030, 1, 1))
    sim.set_price("AAPL", D("101.25"))
    marks = sim.get_marks(["AAPL", "MSFT"])
    assert list(marks) == ["AAPL"] and marks["AAPL"].price == D("101.25")
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-MIN)


# ------------------------------------------------------ faults & invalid data


@pytest.mark.parametrize(
    "error",
    [RateLimitedError("slow down"), ServiceUnavailableError("503"),
     AuthenticationError("401", status_code=401)],
)  # fmt: skip
def test_injected_read_faults_fire_once(error: Exception) -> None:
    sim, _ = make_sim()
    sim.fail_next("get_positions", error)  # type: ignore[arg-type]
    with pytest.raises(type(error)):
        sim.get_positions()
    assert sim.get_positions() == []


@pytest.mark.parametrize(
    ("method", "mutate", "message"),
    [
        ("get_activities", lambda r: {**r, "qty": 10.0}, "must be a string"),
        ("get_activities", lambda r: {k: v for k, v in r.items() if k != "order_id"},
         "order_id"),
        ("get_activities", lambda r: {**r, "transaction_time": "2026-11-09T15:01:00"},
         "UTC offset"),
        ("get_activities", lambda r: {**r, "side": "short_sell"}, "unknown value"),
        ("get_activities", lambda r: {**r, "price": "NaN"}, "NaN"),
        ("get_activities", lambda r: {**r, "qty": "-1"}, "positive"),
        ("get_order_by_client_id", lambda r: {**r, "filled_qty": 4}, "must be a string"),
        ("get_order_by_client_id", lambda r: {**r, "time_in_force": "ioc"}, "unknown value"),
        ("get_order_by_client_id", lambda r: {**r, "symbol": "aapl"}, "payload invalid"),
        ("get_positions", lambda r: {**r, "avg_entry_price": "1e3x"}, "decimal"),
        ("get_account", lambda r: {**r, "cash": 100000.0}, "must be a string"),
        ("get_account", lambda r: {**r, "trading_blocked": "false"}, "boolean"),
        ("get_account_config", lambda r: {**r, "max_margin_multiplier": None}, "missing"),
    ],
)  # fmt: skip
def test_invalid_broker_data_is_rejected(method: str, mutate: Any, message: str) -> None:
    sim, clock = make_sim()
    req = buy()
    sim.submit_order(req)
    sim.schedule_fill(req.client_order_id, D(10), D(100), after=MIN)
    clock.advance(MIN)
    sim.corrupt_next(method, mutate)
    call = {
        "get_activities": lambda: sim.get_activities(page_token=None),
        "get_order_by_client_id": lambda: sim.get_order_by_client_id(req.client_order_id),
        "get_positions": sim.get_positions,
        "get_account": sim.get_account,
        "get_account_config": sim.get_account_config,
    }[method]
    with pytest.raises(InvalidBrokerDataError, match=message):
        call()
    call()  # the corruption applied once; the next response is clean


# --------------------------------------------- determinism & crash recovery


def _scenario() -> tuple[list[str], list[str], list[Any]]:
    sim, clock = make_sim(activity_delay=30 * timedelta(seconds=1))
    ids = [f"stb-replay-{i}" for i in range(3)]
    sim.script_submit(AcceptThenFail("gateway_timeout"), client_order_id=ids[1])
    sim.submit_order(buy(coid=ids[0]))
    with pytest.raises(AmbiguousSubmissionError):
        sim.submit_order(buy(coid=ids[1], symbol="MSFT"))
    sim.submit_order(buy(coid=ids[2], symbol="SPY"))
    sim.schedule_fill(ids[0], D(4), D("99.50"), after=MIN)
    sim.schedule_fill(ids[1], D(10), D("100"), after=MIN)
    sim.schedule_fill(ids[0], D(6), D("99.75"), after=2 * MIN)
    sim.schedule_expire(ids[2], after=3 * MIN)
    clock.advance(5 * MIN)
    acts = [a.raw_json for a in sim.get_activities(page_token=None).activities]
    orders = sim.get_orders(after=T0 - MIN)
    return sim.event_log, acts, orders


def test_deterministic_replay() -> None:
    first, second = _scenario(), _scenario()
    assert first == second
    log, acts, _ = first
    assert len(acts) == 3 and len(log) >= 7
    # Events run as of their scheduled time even though the clock jumped 5 minutes.
    fill_times = [line[:16] for line in log if " FILL " in line]
    assert fill_times == ["2026-11-09T15:01", "2026-11-09T15:01", "2026-11-09T15:02"]
    assert any("EXPIRED" in line and line.startswith("2026-11-09T15:03") for line in log)


def test_client_crash_recovery_by_lookup_and_feed() -> None:
    """The broker outlives the client: a fresh client recovers state by lookups only."""
    sim, clock = make_sim(order_visibility_delay=2 * MIN)
    req = buy()
    sim.script_submit(AcceptThenFail("gateway_timeout"))
    with pytest.raises(AmbiguousSubmissionError):
        sim.submit_order(req)
    sim.schedule_fill(req.client_order_id, D(10), D(100), after=MIN)
    # -- client process dies here; a new client starts 1 minute later --
    clock.advance(MIN)
    assert sim.get_order_by_client_id(req.client_order_id) is None  # not yet visible
    clock.advance(MIN)
    recovered = sim.get_order_by_client_id(req.client_order_id)
    assert recovered is not None and recovered.broker_status == "filled"
    fills = [a for a in sim.get_activities(page_token=None).activities if a.is_fill]
    assert [f.order_id for f in fills] == [recovered.broker_order_id]
    with pytest.raises(DuplicateClientOrderIdError):
        sim.submit_order(req)


def test_submit_request_validation() -> None:
    with pytest.raises(ValueError, match="whole"):
        buy(qty="1.5")
    with pytest.raises(ValueError, match="limit_price"):
        SubmitOrderRequest("stb-x", "AAPL", Side.BUY, D(1), OrderType.LIMIT, TimeInForce.DAY)
    with pytest.raises(ValueError, match="only limit"):
        SubmitOrderRequest("stb-x", "AAPL", Side.BUY, D(1), OrderType.MARKET, TimeInForce.DAY,
                           D(1))  # fmt: skip
    with pytest.raises(ValueError, match="client_order_id"):
        SubmitOrderRequest("", "AAPL", Side.BUY, D(1), OrderType.MARKET, TimeInForce.DAY)


def test_clock_requires_aware_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(datetime(2026, 11, 9))  # noqa: DTZ001
    assert SimClock(T0).now().tzinfo is UTC
