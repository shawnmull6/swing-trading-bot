"""Explicit mapping from broker order-status strings to local statuses (ARCHITECTURE §3 V6).

The table is exhaustive for the alpaca-py 0.45 ``OrderStatus`` enum (a unit test
enumerates it). Strings are used instead of SDK enums so the domain never imports a
broker SDK. Unknown or unexpected statuses raise; callers convert those into a
persistent halt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.domain.money import ZERO


class Disposition(StrEnum):
    WORKING = "WORKING"
    PENDING_CANCEL = "PENDING_CANCEL"
    TERMINAL = "TERMINAL"
    UNEXPECTED = "UNEXPECTED"


@dataclass(frozen=True, slots=True)
class BrokerStatusRule:
    disposition: Disposition
    terminal_status: OrderStatus | None = None
    attention: bool = False
    """Legal but unusual; surfaced in logs/status for operator review."""


_W = BrokerStatusRule(Disposition.WORKING)
_W_ATTENTION = BrokerStatusRule(Disposition.WORKING, attention=True)

BROKER_STATUS_RULES: Final[Mapping[str, BrokerStatusRule]] = MappingProxyType(
    {
        "new": _W,
        "pending_new": _W,
        "accepted": _W,
        "accepted_for_bidding": _W,
        "held": _W,
        "partially_filled": _W,
        "done_for_day": _W,
        "calculated": _W,
        "stopped": _W_ATTENTION,
        "suspended": _W_ATTENTION,
        "pending_review": _W_ATTENTION,  # deprecated by Alpaca; mapped defensively
        "pending_cancel": BrokerStatusRule(Disposition.PENDING_CANCEL),
        "filled": BrokerStatusRule(Disposition.TERMINAL, OrderStatus.FILLED),
        "canceled": BrokerStatusRule(Disposition.TERMINAL, OrderStatus.CANCELED),
        "expired": BrokerStatusRule(Disposition.TERMINAL, OrderStatus.EXPIRED),
        "rejected": BrokerStatusRule(Disposition.TERMINAL, OrderStatus.REJECTED),
        # The bot never replaces orders in M1; seeing these means something else did.
        "replaced": BrokerStatusRule(Disposition.UNEXPECTED),
        "pending_replace": BrokerStatusRule(Disposition.UNEXPECTED),
    }
)


class BrokerStatusError(RuntimeError):
    """Base class; every subclass leads to a persistent halt."""


class UnmappedBrokerStatusError(BrokerStatusError):
    pass


class UnexpectedBrokerStatusError(BrokerStatusError):
    pass


class InconsistentBrokerOrderError(BrokerStatusError):
    pass


def rule_for(broker_status: str) -> BrokerStatusRule:
    try:
        return BROKER_STATUS_RULES[broker_status]
    except KeyError:
        raise UnmappedBrokerStatusError(f"unmapped broker order status {broker_status!r}") from None


def local_status_for(broker_status: str, *, filled_qty: Decimal, qty: Decimal) -> OrderStatus:
    """Derive the local status from a broker status plus fill progress."""
    if filled_qty < ZERO or filled_qty > qty:
        raise InconsistentBrokerOrderError(
            f"filled_qty {filled_qty} outside [0, {qty}] for status {broker_status!r}"
        )
    rule = rule_for(broker_status)
    match rule.disposition:
        case Disposition.UNEXPECTED:
            raise UnexpectedBrokerStatusError(f"unexpected broker order status {broker_status!r}")
        case Disposition.PENDING_CANCEL:
            return OrderStatus.PENDING_CANCEL
        case Disposition.WORKING:
            if broker_status == "partially_filled" and filled_qty == ZERO:
                raise InconsistentBrokerOrderError("partially_filled with zero filled_qty")
            if filled_qty == qty:
                # Fully filled but status not yet terminal: stay working until the
                # broker confirms; never infer a terminal state.
                return OrderStatus.PARTIALLY_FILLED
            return OrderStatus.ACCEPTED if filled_qty == ZERO else OrderStatus.PARTIALLY_FILLED
        case Disposition.TERMINAL:
            target = rule.terminal_status
            if target is None:  # pragma: no cover - table invariant, tested
                raise UnmappedBrokerStatusError(broker_status)
            if target is OrderStatus.FILLED and filled_qty != qty:
                raise InconsistentBrokerOrderError("filled status with filled_qty != qty")
            if target is OrderStatus.REJECTED and filled_qty != ZERO:
                raise InconsistentBrokerOrderError("rejected status with non-zero filled_qty")
            return target
