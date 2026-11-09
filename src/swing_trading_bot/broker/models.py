"""Broker-side snapshots (ARCHITECTURE §5). Exact Decimals, explicit UTC timestamps.

Order and position snapshots reuse :mod:`swing_trading_bot.domain.models`
(``BrokerOrderSnapshot``, ``BrokerPosition``). Nothing here carries the broker's
buying power: the bot never sizes from it (D8). ``non_marginable_buying_power`` is
the cash-only backstop of §11.1 #14 and is reported, not used for sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from swing_trading_bot.domain.enums import OrderType, Side, TimeInForce
from swing_trading_bot.domain.money import ZERO, is_whole


def _aware(value: datetime, name: str) -> datetime:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    as_of: datetime
    status: str
    currency: str
    cash: Decimal
    non_marginable_buying_power: Decimal
    multiplier: Decimal
    pattern_day_trader: bool
    trading_blocked: bool
    account_blocked: bool
    accrued_fees: Decimal
    pending_reg_taf_fees: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))


@dataclass(frozen=True, slots=True)
class AccountConfigSnapshot:
    no_shorting: bool
    max_margin_multiplier: Decimal
    pdt_check: str
    dtbp_check: str
    suspend_trade: bool
    fractional_trading: bool

    @property
    def m2_protections_in_place(self) -> bool:
        """U6: required before M2 paper execution; reported only in M1."""
        return self.no_shorting and self.max_margin_multiplier == 1


@dataclass(frozen=True, slots=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime

    def __post_init__(self) -> None:
        for name in ("timestamp", "next_open", "next_close"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class BrokerActivity:
    """One account activity. FILLs carry execution detail; non-trade activities (FEE,
    DIV, INT, ...) carry a net amount and a date. Alpaca FILL activities identify the
    order by broker ``order_id`` only (no client_order_id)."""

    activity_id: str
    activity_type: str
    activity_date: date
    occurred_at: datetime | None
    raw_json: str
    symbol: str | None = None
    side: Side | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    net_amount: Decimal | None = None
    order_id: str | None = None
    cum_qty: Decimal | None = None
    leaves_qty: Decimal | None = None
    order_status: str | None = None
    fill_type: str | None = None

    @property
    def is_fill(self) -> bool:
        return self.activity_type == "FILL"


@dataclass(frozen=True, slots=True)
class ActivityPage:
    activities: tuple[BrokerActivity, ...]
    page_size: int

    @property
    def last_id(self) -> str | None:
        return self.activities[-1].activity_id if self.activities else None

    @property
    def possibly_more(self) -> bool:
        """A full page may be followed by more; confirm with one more request (§8.4)."""
        return len(self.activities) == self.page_size


@dataclass(frozen=True, slots=True)
class SubmitOrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id or len(self.client_order_id) > 128:
            raise ValueError("client_order_id must be 1..128 characters")
        if self.qty <= ZERO or not is_whole(self.qty):
            raise ValueError("qty must be a positive whole number of shares")
        if self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            if self.limit_price is None or self.limit_price <= ZERO:
                raise ValueError("limit orders need a positive limit_price")
        elif self.limit_price is not None:
            raise ValueError("only limit orders carry limit_price")


@dataclass(frozen=True, slots=True)
class BrokerSessionClose:
    """A symbol's daily-bar close for one session, as reported by market data. Whether
    it equals the official (consolidated) close depends on the feed (see §12.10)."""

    symbol: str
    session_date: date
    close: Decimal
    as_of: datetime
    received_at: datetime
    feed: str

    def __post_init__(self) -> None:
        if self.close <= ZERO:
            raise ValueError("close must be positive")
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "received_at", _aware(self.received_at, "received_at"))
