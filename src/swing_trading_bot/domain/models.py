"""Immutable domain models.

All models are frozen, reject unknown fields, reject floats for numeric fields and
require timezone-aware datetimes (normalised to UTC). Broker adapters (step 9) map
raw payloads into these types; nothing here knows about a specific broker SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from swing_trading_bot.domain.enums import (
    IntentPurpose,
    MarkSource,
    OrderType,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.money import (
    DecimalValue,
    NonNegativeDecimal,
    OrderPrice,
    PositiveDecimal,
    Shares,
)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]

Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z]{1,2})?$")]
"""US equity/ETF ticker, upper-case, optional share-class suffix (e.g. ``BRK.B``)."""

IntentKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_.\-]+$")
]


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class OrderIntent(DomainModel):
    """A request to trade, before risk checks and before any reservation.

    Structural rules enforced here (risk limits are applied later by the risk gate):

    * whole shares only (D3);
    * buys are limit orders with a planned stop strictly below the limit (D4, D14);
    * buys are always ``ENTRY``; sells are exits;
    * limit orders carry a limit price, market orders do not.
    """

    intent_key: IntentKey
    symbol: Symbol
    side: Side
    qty: Shares
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: OrderPrice | None = None
    planned_stop: OrderPrice | None = None
    purpose: IntentPurpose
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _check_structure(self) -> Self:
        if self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            if self.limit_price is None:
                raise ValueError("limit orders require limit_price")
        elif self.limit_price is not None:
            raise ValueError(f"{self.order_type} orders must not carry limit_price")
        if self.side is Side.BUY:
            if self.order_type is not OrderType.LIMIT:
                raise ValueError("buy orders must be LIMIT orders (D4)")
            if self.purpose is not IntentPurpose.ENTRY:
                raise ValueError("buy orders must have purpose ENTRY (long-only)")
            if self.planned_stop is None:
                raise ValueError("buy orders require a planned_stop")
            if self.limit_price is None:  # pragma: no cover - guaranteed by LIMIT check
                raise ValueError("limit orders require limit_price")
            if self.planned_stop >= self.limit_price:
                raise ValueError("planned_stop must be below limit_price for a long entry")
        elif self.purpose is IntentPurpose.ENTRY:
            raise ValueError("sell orders cannot be ENTRY (no shorting)")
        return self

    @property
    def max_notional(self) -> Decimal | None:
        """Worst-case cost/proceeds at the limit price, before fees."""
        return None if self.limit_price is None else self.qty * self.limit_price

    @property
    def planned_risk(self) -> Decimal | None:
        """(entry - stop) x qty for a long entry; None for exits."""
        if self.limit_price is None or self.planned_stop is None:
            return None
        return (self.limit_price - self.planned_stop) * self.qty


class PriceMark(DomainModel):
    """A timestamped price observation (ARCHITECTURE §6.6). Freshness is policy, not shape."""

    symbol: Symbol
    price: PositiveDecimal
    as_of: UtcDatetime
    received_at: UtcDatetime
    source: MarkSource
    feed: str | None = None


class BrokerOrderSnapshot(DomainModel):
    """Broker-reported order state. Broker truth: only structural checks here."""

    client_order_id: str = Field(min_length=1, max_length=128)
    broker_order_id: str = Field(min_length=1)
    symbol: Symbol
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    qty: PositiveDecimal
    filled_qty: NonNegativeDecimal
    filled_avg_price: PositiveDecimal | None = None
    limit_price: PositiveDecimal | None = None
    broker_status: str = Field(min_length=1)
    submitted_at: UtcDatetime | None = None
    updated_at: UtcDatetime


class ExecutionRecord(DomainModel):
    """One broker-confirmed execution (from a FILL account activity)."""

    activity_id: str = Field(min_length=1)
    broker_order_id: str = Field(min_length=1)
    symbol: Symbol
    side: Side
    qty: PositiveDecimal
    price: PositiveDecimal
    cum_qty: PositiveDecimal
    leaves_qty: NonNegativeDecimal
    transaction_time: UtcDatetime


class BrokerPosition(DomainModel):
    """Broker-reported position. ``qty`` may be negative: broker truth is never rejected."""

    symbol: Symbol
    qty: DecimalValue
    avg_entry_price: NonNegativeDecimal
