"""Deterministic in-memory broker for tests and ``simulate`` mode (ARCHITECTURE §5.2-5.3).

``SimBroker`` implements both :class:`BrokerReadPort` and :class:`BrokerExecutionPort`.
It is entirely in memory: it has no database, no network and no randomness.

Determinism
-----------
* Time comes only from an injected :class:`SimClock`. Scheduled events (fills,
  cancels, expirations, fees, dividends) run in ``(time, sequence)`` order the next
  time any port method is called at or after their time.
* Identifiers derive from a sequence counter: broker order ids are UUIDs built from it,
  activity ids are ``<UTC yyyymmddHHMMSSffffff>::<uuid>`` (Alpaca's sortable shape).
* ``event_log`` records every state change, so two runs of the same script can be
  compared exactly.

Realism hooks
-------------
* Scripted submit outcomes: accept, definitive reject, accept-then-fail (504 /
  timeout / reset after the order was created), fail-before-receipt, rate limit, auth
  failure. Duplicate ``client_order_id`` raises ``DuplicateClientOrderIdError``.
* Order-visibility delay: an accepted order can be invisible to lookups for a while.
* Activity-reporting delay: an execution can appear in the activity feed after later
  executions — exactly the situation the overlap re-scan of §8.4 exists for.
* Read faults (rate limit, unavailable, auth) and payload corruption are injectable.
* All responses are produced as Alpaca-shaped JSON and parsed by
  :mod:`swing_trading_bot.broker.mapping`, so invalid data is caught the same way it
  will be for the real adapter.
"""

from __future__ import annotations

import heapq
import itertools
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal
from uuid import UUID

from swing_trading_bot.broker.errors import (
    AmbiguousSubmissionError,
    AuthenticationError,
    BrokerError,
    DuplicateClientOrderIdError,
    NotCancelableError,
    OrderRejectedError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.mapping import (
    account_config_from_raw,
    account_from_raw,
    activity_from_raw,
    clock_from_raw,
    order_from_raw,
    position_from_raw,
)
from swing_trading_bot.broker.models import (
    AccountConfigSnapshot,
    AccountSnapshot,
    ActivityPage,
    MarketClock,
    SubmitOrderRequest,
)
from swing_trading_bot.broker.ports import MAX_ACTIVITY_PAGE_SIZE
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarFetch, CalendarKind
from swing_trading_bot.domain.enums import MarkSource, OrderType, Side
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition, PriceMark
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, PRICE_QUANTUM, ZERO

Raw = dict[str, Any]
Mutator = Callable[[Raw], Raw]

ENVIRONMENT = "sim"
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})


class SimScriptError(ValueError):
    """The test script asked for something a real broker could not do."""


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.utcoffset() is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("time cannot move backwards")
        self._now += delta
        return self._now

    def advance_to(self, when: datetime) -> datetime:
        return self.advance(when.astimezone(UTC) - self._now)


# --------------------------------------------------------------- submit script


@dataclass(frozen=True, slots=True)
class Accept:
    visibility_delay: timedelta | None = None
    initial_status: str = "new"


@dataclass(frozen=True, slots=True)
class Reject:
    reason: str = "rejected by broker"
    status_code: int = 422


Failure = Literal["gateway_timeout", "read_timeout", "connection_reset"]


@dataclass(frozen=True, slots=True)
class AcceptThenFail:
    """The broker creates the order, but the client sees a transport failure."""

    failure: Failure = "gateway_timeout"
    visibility_delay: timedelta | None = None


@dataclass(frozen=True, slots=True)
class FailBeforeReceipt:
    """The request never reaches the broker; the client sees the same failure."""

    failure: Failure = "connection_reset"


@dataclass(frozen=True, slots=True)
class RateLimit:
    retry_after: timedelta = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class AuthFailure:
    status_code: int = 401


SubmitBehavior = Accept | Reject | AcceptThenFail | FailBeforeReceipt | RateLimit | AuthFailure

_FAILURE_STATUS: dict[Failure, int | None] = {
    "gateway_timeout": 504,
    "read_timeout": None,
    "connection_reset": None,
}


# --------------------------------------------------------------------- config


@dataclass(frozen=True, slots=True)
class SimConfig:
    start_cash: Decimal = Decimal("100000")
    margin_multiplier: Decimal = Decimal("4")
    order_visibility_delay: timedelta = timedelta(0)
    activity_delay: timedelta = timedelta(0)
    cancel_delay: timedelta = timedelta(0)
    no_shorting: bool = True
    max_margin_multiplier: Decimal = Decimal("1")
    pdt_check: str = "entry"
    dtbp_check: str = "entry"
    currency: str = "USD"
    account_id: str = "sim-account-0001"
    account_status: str = "ACTIVE"
    trading_blocked: bool = False
    suspend_trade: bool = False


# ---------------------------------------------------------------- internal state


@dataclass(slots=True)
class _Order:
    broker_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    tif: str
    qty: Decimal
    limit_price: Decimal | None
    status: str
    created_at: datetime
    updated_at: datetime
    visible_at: datetime
    filled_qty: Decimal = ZERO
    notional: Decimal = ZERO

    @property
    def leaves(self) -> Decimal:
        return self.qty - self.filled_qty


@dataclass(slots=True)
class _Activity:
    raw: Raw
    visible_at: datetime
    occurred_at: datetime

    @property
    def activity_id(self) -> str:
        return str(self.raw["id"])


@dataclass(order=True, slots=True)
class _Event:
    at: datetime
    seq: int
    action: Callable[[], None] = field(compare=False)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _num(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value, "f")


class SimBroker:
    """Deterministic broker simulator implementing both broker ports."""

    def __init__(self, clock: SimClock, config: SimConfig | None = None) -> None:
        self.clock = clock
        self.config = config or SimConfig()
        self._seq = itertools.count(1)
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, cost)
        self._cash = self.config.start_cash
        self._activities: list[_Activity] = []
        self._events: list[_Event] = []
        self._submit_queue: deque[SubmitBehavior] = deque()
        self._submit_by_client: dict[str, SubmitBehavior] = {}
        self._faults: dict[str, deque[BrokerError]] = {}
        self._mutators: dict[str, deque[Mutator]] = {}
        self._calendars: dict[CalendarKind, CalendarFetch] = {}
        self._prices: dict[str, tuple[Decimal, datetime]] = {}
        self.event_log: list[str] = []
        self._event_time: datetime | None = None

    # ============================================================ scripting
    def script_submit(
        self, behavior: SubmitBehavior, *, client_order_id: str | None = None
    ) -> None:
        """Queue the outcome of a future submit (per client id, else FIFO)."""
        if client_order_id is None:
            self._submit_queue.append(behavior)
        else:
            self._submit_by_client[client_order_id] = behavior

    def fail_next(self, method: str, error: BrokerError) -> None:
        """Make the next call of a read method raise ``error``."""
        self._faults.setdefault(method, deque()).append(error)

    def corrupt_next(self, method: str, mutate: Mutator) -> None:
        """Mutate the next raw payload(s) a read method returns before mapping."""
        self._mutators.setdefault(method, deque()).append(mutate)

    def load_calendar(self, fetch: CalendarFetch) -> None:
        self._calendars[fetch.kind] = fetch

    def set_price(self, symbol: str, price: Decimal) -> None:
        self._prices[symbol] = (price, self.clock.now())

    def schedule_fill(
        self,
        client_order_id: str,
        qty: Decimal,
        price: Decimal,
        *,
        after: timedelta = timedelta(0),
        activity_delay: timedelta | None = None,
    ) -> None:
        order = self._order_for_script(client_order_id)
        if qty <= ZERO or price <= ZERO:
            raise SimScriptError("fill qty and price must be positive")
        limit = order.limit_price
        if limit is not None and (
            (order.side is Side.BUY and price > limit)
            or (order.side is Side.SELL and price < limit)
        ):
            raise SimScriptError(f"fill price {price} violates limit {limit}")
        delay = self.config.activity_delay if activity_delay is None else activity_delay
        self._schedule(after, lambda: self._fill(order.broker_id, qty, price, delay))

    def schedule_expire(self, client_order_id: str, *, after: timedelta) -> None:
        order = self._order_for_script(client_order_id)
        self._schedule(after, lambda: self._finish(order.broker_id, "expired"))

    def schedule_broker_cancel(self, client_order_id: str, *, after: timedelta) -> None:
        order = self._order_for_script(client_order_id)
        self._schedule(after, lambda: self._finish(order.broker_id, "canceled"))

    def schedule_reject(self, client_order_id: str, *, after: timedelta) -> None:
        order = self._order_for_script(client_order_id)
        self._schedule(after, lambda: self._reject_accepted(order.broker_id))

    def schedule_cash_activity(
        self,
        activity_type: Literal["FEE", "DIV"],
        amount: Decimal,
        *,
        symbol: str | None = None,
        after: timedelta = timedelta(0),
        activity_delay: timedelta | None = None,
    ) -> None:
        delay = self.config.activity_delay if activity_delay is None else activity_delay
        self._schedule(after, lambda: self._cash_activity(activity_type, amount, symbol, delay))

    # =========================================================== read port
    def get_account(self) -> AccountSnapshot:
        self._enter("get_account")
        raw = {
            "id": self.config.account_id,
            "status": self.config.account_status,
            "currency": self.config.currency,
            "cash": _num(self._cash),
            "non_marginable_buying_power": _num(max(ZERO, self._cash)),
            "buying_power": _num(self._cash * self.config.margin_multiplier),  # never read
            "multiplier": _num(self.config.margin_multiplier),
            "pattern_day_trader": False,
            "trading_blocked": self.config.trading_blocked,
            "account_blocked": False,
            "accrued_fees": "0",
            "pending_reg_taf_fees": "0",
        }
        return account_from_raw(
            self._mutate("get_account", raw), as_of=self.clock.now(), environment=ENVIRONMENT
        )

    def get_account_config(self) -> AccountConfigSnapshot:
        self._enter("get_account_config")
        raw = {
            "no_shorting": self.config.no_shorting,
            "max_margin_multiplier": _num(self.config.max_margin_multiplier),
            "pdt_check": self.config.pdt_check,
            "dtbp_check": self.config.dtbp_check,
            "suspend_trade": self.config.suspend_trade,
            "fractional_trading": False,
        }
        return account_config_from_raw(self._mutate("get_account_config", raw))

    def get_clock(self) -> MarketClock:
        self._enter("get_clock")
        now = self.clock.now()
        trading = self._calendars.get(CalendarKind.TRADING)
        if trading is None:
            raise ServiceUnavailableError("sim: no trading calendar loaded")
        sessions = sorted(trading.sessions, key=lambda s: s.session_date)
        is_open = any(s.open_at <= now < s.close_at for s in sessions)
        next_open = next((s.open_at for s in sessions if s.open_at > now), None)
        next_close = next((s.close_at for s in sessions if s.close_at > now), None)
        if next_open is None or next_close is None:
            raise ServiceUnavailableError("sim: calendar does not extend past now")
        raw = {"timestamp": _iso(now), "is_open": is_open, "next_open": _iso(next_open),
               "next_close": _iso(next_close)}  # fmt: skip
        return clock_from_raw(self._mutate("get_clock", raw))

    def get_calendar(self, kind: CalendarKind, start: date, end: date) -> CalendarFetch:
        self._enter("get_calendar")
        loaded = self._calendars.get(kind)
        if loaded is None or start < loaded.start or end > loaded.end:
            raise ServiceUnavailableError(f"sim: {kind.value} calendar not available for range")
        return CalendarFetch(
            kind=kind,
            start=start,
            end=end,
            fetched_at=self.clock.now(),
            source="sim",
            sessions=tuple(s for s in loaded.sessions if start <= s.session_date <= end),
            settlement_days=tuple(d for d in loaded.settlement_days if start <= d <= end),
        )

    def get_positions(self) -> list[BrokerPosition]:
        self._enter("get_positions")
        out = []
        for symbol in sorted(self._positions):
            qty, cost = self._positions[symbol]
            avg = DECIMAL_CONTEXT.divide(cost, qty).quantize(
                PRICE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
            )
            raw = {"symbol": symbol, "qty": _num(qty), "avg_entry_price": _num(avg),
                   "side": "long" if qty > 0 else "short"}  # fmt: skip
            out.append(position_from_raw(self._mutate("get_positions", raw)))
        return out

    def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        self._enter("get_open_orders")
        return [
            order_from_raw(self._mutate("get_open_orders", self._order_raw(o)))
            for o in self._visible_orders()
            if o.status not in TERMINAL
        ]

    def get_orders(
        self, *, after: datetime, until: datetime | None = None
    ) -> list[BrokerOrderSnapshot]:
        self._enter("get_orders")
        return [
            order_from_raw(self._mutate("get_orders", self._order_raw(o)))
            for o in self._visible_orders()
            if o.created_at > after and (until is None or o.created_at <= until)
        ]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        self._enter("get_order_by_client_id")
        broker_id = self._by_client.get(client_order_id)
        if broker_id is None:
            return None
        order = self._orders[broker_id]
        if order.visible_at > self.clock.now():
            return None
        return order_from_raw(self._mutate("get_order_by_client_id", self._order_raw(order)))

    def get_activities(
        self,
        *,
        page_token: str | None,
        page_size: int = MAX_ACTIVITY_PAGE_SIZE,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> ActivityPage:
        self._enter("get_activities")
        if not 1 <= page_size <= MAX_ACTIVITY_PAGE_SIZE:
            raise ValueError(f"page_size must be within 1..{MAX_ACTIVITY_PAGE_SIZE}")
        now = self.clock.now()
        visible = sorted(
            (a for a in self._activities if a.visible_at <= now), key=lambda a: a.activity_id
        )
        selected = [
            a
            for a in visible
            if (page_token is None or a.activity_id > page_token)
            and (after is None or a.occurred_at > after)
            and (until is None or a.occurred_at <= until)
        ][:page_size]
        return ActivityPage(
            activities=tuple(
                activity_from_raw(self._mutate("get_activities", dict(a.raw))) for a in selected
            ),
            page_size=page_size,
        )

    def get_marks(self, symbols: Sequence[str]) -> Mapping[str, PriceMark]:
        self._enter("get_marks")
        now = self.clock.now()
        return {
            sym: PriceMark(
                symbol=sym,
                price=price,
                as_of=as_of,
                received_at=now,
                source=MarkSource.SIM,
                feed="sim",
            )
            for sym in symbols
            if sym in self._prices
            for price, as_of in [self._prices[sym]]
        }

    # ====================================================== execution port
    def submit_order(self, request: SubmitOrderRequest) -> BrokerOrderSnapshot:
        self._tick()
        behavior = self._submit_by_client.pop(request.client_order_id, None)
        if behavior is None:
            behavior = self._submit_queue.popleft() if self._submit_queue else Accept()
        coid = request.client_order_id
        match behavior:
            case RateLimit(retry_after=_):
                # A 429 on a write never proves non-acceptance: report it as ambiguous.
                self._log(f"SUBMIT {coid} rate-limited (ambiguous)")
                raise AmbiguousSubmissionError("sim: HTTP 429 on submit", status_code=429)
            case AuthFailure(status_code=code):
                self._log(f"SUBMIT {coid} auth-failed")
                raise AuthenticationError("sim: unauthorized", status_code=code)
            case FailBeforeReceipt(failure=failure):
                self._log(f"SUBMIT {coid} lost-before-receipt ({failure})")
                raise AmbiguousSubmissionError(
                    f"sim: {failure}", status_code=_FAILURE_STATUS[failure]
                )
            case _:
                pass
        if coid in self._by_client:
            self._log(f"SUBMIT {coid} duplicate")
            raise DuplicateClientOrderIdError(
                "sim: client_order_id must be unique", status_code=422
            )
        if isinstance(behavior, Reject):
            self._log(f"SUBMIT {coid} rejected ({behavior.reason})")
            raise OrderRejectedError(f"sim: {behavior.reason}", status_code=behavior.status_code)
        self._check_business_rules(request)
        now = self.clock.now()
        delay = getattr(behavior, "visibility_delay", None)
        visibility = self.config.order_visibility_delay if delay is None else delay
        order = _Order(
            broker_id=str(UUID(int=next(self._seq))),
            client_order_id=coid,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            tif=request.time_in_force.value.lower(),
            qty=request.qty,
            limit_price=request.limit_price,
            status=behavior.initial_status if isinstance(behavior, Accept) else "new",
            created_at=now,
            updated_at=now,
            visible_at=now + visibility,
        )
        self._orders[order.broker_id] = order
        self._by_client[coid] = order.broker_id
        self._log(f"SUBMIT {coid} accepted as {order.broker_id} ({order.status})")
        if isinstance(behavior, AcceptThenFail):
            self._log(f"SUBMIT {coid} response lost ({behavior.failure})")
            raise AmbiguousSubmissionError(
                f"sim: {behavior.failure}", status_code=_FAILURE_STATUS[behavior.failure]
            )
        return order_from_raw(self._order_raw(order))

    def cancel_order(self, broker_order_id: str) -> None:
        self._tick()
        order = self._orders.get(broker_order_id)
        if order is None or order.visible_at > self.clock.now():
            raise NotCancelableError("sim: order not found", status_code=404)
        if order.status in TERMINAL:
            raise NotCancelableError(f"sim: order is {order.status}", status_code=422)
        order.status = "pending_cancel"
        order.updated_at = self.clock.now()
        self._log(f"CANCEL {order.client_order_id} requested")
        self._schedule(self.config.cancel_delay, lambda: self._finish(broker_order_id, "canceled"))

    # ========================================================== internals
    def _check_business_rules(self, request: SubmitOrderRequest) -> None:
        if request.side is Side.SELL and self.config.no_shorting:
            held = self._positions.get(request.symbol, (ZERO, ZERO))[0]
            reserved = sum(
                (
                    o.leaves
                    for o in self._orders.values()
                    if o.symbol == request.symbol
                    and o.side is Side.SELL
                    and o.status not in TERMINAL
                ),
                ZERO,
            )
            if request.qty > held - reserved:
                self._log(f"SUBMIT {request.client_order_id} rejected (insufficient qty)")
                raise OrderRejectedError("sim: insufficient qty available", status_code=403)
        if request.side is Side.BUY and request.limit_price is not None:
            power = self._cash * self.config.margin_multiplier
            if request.qty * request.limit_price > power:
                self._log(f"SUBMIT {request.client_order_id} rejected (insufficient funds)")
                raise OrderRejectedError("sim: insufficient buying power", status_code=403)

    def _order_for_script(self, client_order_id: str) -> _Order:
        broker_id = self._by_client.get(client_order_id)
        if broker_id is None:
            raise SimScriptError(f"no order {client_order_id} at the broker")
        return self._orders[broker_id]

    def _schedule(self, after: timedelta, action: Callable[[], None]) -> None:
        if after < timedelta(0):
            raise SimScriptError("cannot schedule in the past")
        heapq.heappush(self._events, _Event(self.clock.now() + after, next(self._seq), action))

    def _tick(self) -> None:
        """Run due events in (time, sequence) order, each AS OF its scheduled time, so
        timestamps and delays do not depend on when the simulator is next polled."""
        now = self.clock.now()
        while self._events and self._events[0].at <= now:
            event = heapq.heappop(self._events)
            self._event_time = event.at
            try:
                event.action()
            finally:
                self._event_time = None

    def _at(self) -> datetime:
        return self._event_time if self._event_time is not None else self.clock.now()

    def _enter(self, method: str) -> None:
        self._tick()
        faults = self._faults.get(method)
        if faults:
            raise faults.popleft()

    def _mutate(self, method: str, raw: Raw) -> Raw:
        mutators = self._mutators.get(method)
        return mutators.popleft()(raw) if mutators else raw

    def _visible_orders(self) -> list[_Order]:
        now = self.clock.now()
        return sorted(
            (o for o in self._orders.values() if o.visible_at <= now),
            key=lambda o: (o.created_at, o.broker_id),
        )

    def _order_raw(self, o: _Order) -> Raw:
        avg = (
            None
            if o.filled_qty == ZERO
            else DECIMAL_CONTEXT.divide(o.notional, o.filled_qty).quantize(
                PRICE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
            )
        )
        return {
            "id": o.broker_id,
            "client_order_id": o.client_order_id,
            "symbol": o.symbol,
            "side": o.side.value.lower(),
            "type": o.order_type.value.lower(),
            "time_in_force": o.tif,
            "qty": _num(o.qty),
            "filled_qty": _num(o.filled_qty),
            "filled_avg_price": None if avg is None else _num(avg),
            "limit_price": None if o.limit_price is None else _num(o.limit_price),
            "status": o.status,
            "created_at": _iso(o.created_at),
            "submitted_at": _iso(o.created_at),
            "updated_at": _iso(o.updated_at),
        }

    def _fill(self, broker_id: str, qty: Decimal, price: Decimal, delay: timedelta) -> None:
        order = self._orders[broker_id]
        if order.status in TERMINAL:  # e.g. a cancel won the race against this fill
            self._log(f"FILL {order.client_order_id} ignored ({order.status})")
            return
        if qty > order.leaves:
            raise SimScriptError(f"fill {qty} exceeds leaves {order.leaves}")
        now = self._at()
        order.filled_qty += qty
        order.notional += qty * price
        order.status = "filled" if order.leaves == ZERO else "partially_filled"
        order.updated_at = now
        held, cost = self._positions.get(order.symbol, (ZERO, ZERO))
        if order.side is Side.BUY:
            self._cash -= qty * price
            held, cost = held + qty, cost + qty * price
        else:
            self._cash += qty * price
            removed = cost if qty >= held else DECIMAL_CONTEXT.divide(cost * qty, held)
            held, cost = held - qty, cost - removed
        if held == ZERO:
            self._positions.pop(order.symbol, None)
        else:
            self._positions[order.symbol] = (held, cost)
        raw = {
            "id": self._activity_id(now),
            "activity_type": "FILL",
            "transaction_time": _iso(now),
            "type": "fill" if order.leaves == ZERO else "partial_fill",
            "price": _num(price),
            "qty": _num(qty),
            "side": order.side.value.lower(),
            "symbol": order.symbol,
            "leaves_qty": _num(order.leaves),
            "cum_qty": _num(order.filled_qty),
            "order_id": order.broker_id,
            "order_status": order.status,
        }
        self._activities.append(_Activity(raw, now + delay, now))
        self._log(f"FILL {order.client_order_id} {qty}@{price} -> {order.status}")

    def _finish(self, broker_id: str, status: str) -> None:
        order = self._orders[broker_id]
        if order.status in TERMINAL:
            self._log(f"{status.upper()} {order.client_order_id} ignored ({order.status})")
            return
        order.status = status
        order.updated_at = self._at()
        self._log(f"{status.upper()} {order.client_order_id}")

    def _reject_accepted(self, broker_id: str) -> None:
        order = self._orders[broker_id]
        if order.filled_qty != ZERO:
            raise SimScriptError("cannot reject an order that has fills")
        self._finish(broker_id, "rejected")

    def _cash_activity(
        self, activity_type: str, amount: Decimal, symbol: str | None, delay: timedelta
    ) -> None:
        now = self._at()
        self._cash += amount
        raw: Raw = {
            "id": self._activity_id(now),
            "activity_type": activity_type,
            "date": now.astimezone(MARKET_TZ).date().isoformat(),
            "created_at": _iso(now),
            "net_amount": _num(amount),
            "status": "executed",
        }
        if symbol is not None:
            raw["symbol"] = symbol
        self._activities.append(_Activity(raw, now + delay, now))
        self._log(f"{activity_type} {amount}")

    def _activity_id(self, at: datetime) -> str:
        return f"{at.astimezone(UTC):%Y%m%d%H%M%S%f}::{UUID(int=next(self._seq))}"

    def _log(self, message: str) -> None:
        self.event_log.append(f"{_iso(self._at())} {message}")
