"""Broker ports (ARCHITECTURE §5.1-5.2).

Reading and executing are separate protocols. In M1 the only implementation of
:class:`BrokerExecutionPort` is :class:`~swing_trading_bot.broker.sim.SimBroker`; the
Alpaca integration (step 9) implements :class:`BrokerReadPort` only, so the
production runtime cannot construct anything that submits or cancels orders.

Contracts every implementation must honour:

* all money and quantities are exact ``Decimal``; all timestamps timezone-aware UTC;
* malformed broker data raises :class:`~swing_trading_bot.broker.errors.InvalidBrokerDataError`;
* ``get_order_by_client_id`` returns ``None`` when the order is not (yet) visible —
  absence is not proof of non-existence;
* ``submit_order`` raises ``AmbiguousSubmissionError`` for any outcome in which the
  broker may have received the request; ``DuplicateClientOrderIdError`` means an order
  with that id exists;
* activity ids are **opaque strings**: callers never compare, sort, parse or assume
  contiguity of them. ``page_token`` is an opaque cursor whose meaning is defined by
  the implementation (SimBroker: "ids after this one"; Alpaca: see the read adapter).
  ``after``/``until`` bound a time window so overlapping windows can be re-read, and
  ingestion is replay-safe because the ledger deduplicates by activity id;
* read retries are bounded and per operation; writes are never retried on the basis
  of an error class (see ``broker.errors``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from swing_trading_bot.broker.models import (
    AccountConfigSnapshot,
    AccountSnapshot,
    ActivityPage,
    MarketClock,
    SubmitOrderRequest,
)
from swing_trading_bot.domain.calendar import CalendarFetch, CalendarKind
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition, PriceMark

MAX_ACTIVITY_PAGE_SIZE = 100


@runtime_checkable
class BrokerReadPort(Protocol):
    def get_account(self) -> AccountSnapshot: ...

    def get_account_config(self) -> AccountConfigSnapshot: ...

    def get_clock(self) -> MarketClock: ...

    def get_calendar(self, kind: CalendarKind, start: date, end: date) -> CalendarFetch: ...

    def get_positions(self) -> list[BrokerPosition]: ...

    def get_open_orders(self) -> list[BrokerOrderSnapshot]: ...

    def get_orders(
        self, *, after: datetime, until: datetime | None = None
    ) -> list[BrokerOrderSnapshot]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None: ...

    def get_activities(
        self,
        *,
        page_token: str | None,
        page_size: int = MAX_ACTIVITY_PAGE_SIZE,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> ActivityPage: ...


@runtime_checkable
class BrokerExecutionPort(Protocol):
    def submit_order(self, request: SubmitOrderRequest) -> BrokerOrderSnapshot: ...

    def cancel_order(self, broker_order_id: str) -> None: ...


@runtime_checkable
class PriceMarkSource(Protocol):
    def get_marks(self, symbols: Sequence[str]) -> Mapping[str, PriceMark]: ...
