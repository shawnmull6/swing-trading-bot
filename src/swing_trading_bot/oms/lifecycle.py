"""Applying broker-reported order state to a local order (ARCHITECTURE §6.4, §10).

Shared by submission (tx B), synchronization and recovery (tx F). Every change goes
through the domain state machine, is written together with an ``order_events`` audit
row, and — for a broker-confirmed terminal status — closes the reservation in the same
transaction (tx D). Anything that cannot be applied safely raises a persistent halt and
leaves the order untouched.

Fill gating: a terminal status is applied only once every execution the broker reports
(``filled_qty``) has been booked locally from FILL activities. Until then the order
stays working, so the remaining reservation is released only after a broker-confirmed
terminal status **and** the final fill quantity are both known. The broker's figure is
stored (``orders.broker_filled_qty``); while it exceeds the booked quantity, gates and
the risk engine block all new risk (accounting incomplete) and no cash, position or
settlement effect is inferred from the unbooked executions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    InvalidBrokerDataError,
    UnknownBrokerStatusError,
)
from swing_trading_bot.domain.broker_status import (
    BrokerStatusError,
    UnexpectedBrokerStatusError,
    UnmappedBrokerStatusError,
    local_status_for,
)
from swing_trading_bot.domain.enums import HaltReason, OrderStatus
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.domain.state_machine import (
    AMBIGUOUS_STATUSES,
    IllegalTransitionError,
    is_terminal,
    transition,
)
from swing_trading_bot.ledger.db import Tx
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrderEvent,
    OrderEventsRepository,
    OrderProgress,
    OrderRecord,
    OrdersRepository,
    TradingStateRepository,
)

OMS_ACTOR = "oms"


class EventSource(StrEnum):
    LOCAL = "LOCAL"
    BROKER = "BROKER"
    RECOVERY = "RECOVERY"
    OPERATOR = "OPERATOR"


class ApplyOutcome(StrEnum):
    UNCHANGED = "UNCHANGED"
    TRANSITIONED = "TRANSITIONED"
    AWAITING_FILLS = "AWAITING_FILLS"
    HALTED = "HALTED"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: ApplyOutcome
    status: OrderStatus
    released: bool = False
    halt_reason: HaltReason | None = None


def record_event(
    tx: Tx,
    order_id: str,
    *,
    at: datetime,
    source: EventSource,
    event: str,
    from_status: OrderStatus | None = None,
    to_status: OrderStatus | None = None,
    **detail: object,
) -> None:
    OrderEventsRepository().append(
        tx,
        OrderEvent(
            client_order_id=order_id,
            ts=at,
            source=source.value,
            from_status=from_status,
            to_status=to_status,
            detail={"event": event, **detail},
        ),
    )


def read_error_halt(exc: BaseException) -> HaltReason | None:
    """The persistent halt a failed broker READ requires (§10.5), or None when it is
    transient (retry later; record and carry on)."""
    if isinstance(exc, AuthenticationError):
        return HaltReason.AUTH
    if isinstance(exc, UnknownBrokerStatusError):
        return HaltReason.UNMAPPED_BROKER_STATUS
    if isinstance(exc, InvalidBrokerDataError):
        return HaltReason.BROKER_PAYLOAD
    return None


def halt(tx: Tx, reason: HaltReason, detail: str, at: datetime) -> None:
    TradingStateRepository().halt(tx, reason, detail=detail[:2000], actor=OMS_ACTOR, at=at)


def change_status(
    tx: Tx,
    order: OrderRecord,
    target: OrderStatus,
    *,
    portfolio: VirtualPortfolio,
    at: datetime,
    source: EventSource,
    event: str,
    progress: OrderProgress | None = None,
    **detail: object,
) -> bool:
    """Validated transition + audit row; closes the reservation on a terminal target.
    Returns True when a reservation was released or consumed."""
    OrdersRepository().transition_status(
        tx, order.client_order_id, expected=order.status, target=target, at=at, progress=progress
    )
    record_event(tx, order.client_order_id, at=at, source=source, event=event,
                 from_status=order.status, to_status=target, **detail)  # fmt: skip
    if is_terminal(target) and not is_terminal(order.status):
        portfolio.close_reservation(tx, order.client_order_id, now=at)
        return True
    return False


def _identity_mismatch(order: OrderRecord, snap: BrokerOrderSnapshot) -> str | None:
    pairs = {
        "client_order_id": (order.client_order_id, snap.client_order_id),
        "symbol": (order.symbol, snap.symbol),
        "side": (order.side, snap.side),
        "qty": (order.qty, snap.qty),
        "order_type": (order.order_type, snap.order_type),
        "limit_price": (order.limit_price, snap.limit_price),
    }
    if order.broker_order_id is not None:
        pairs["broker_order_id"] = (order.broker_order_id, snap.broker_order_id)
    bad = [f"{k}: local {a} vs broker {b}" for k, (a, b) in pairs.items() if a != b]
    return "; ".join(bad) if bad else None


def apply_snapshot(  # noqa: PLR0911 - each early return is a distinct halt
    tx: Tx,
    order: OrderRecord,
    snap: BrokerOrderSnapshot,
    *,
    portfolio: VirtualPortfolio,
    at: datetime,
    source: EventSource,
) -> ApplyResult:
    """Bring ``order`` in line with the broker's view of it, inside ``tx``."""
    current = OrdersRepository().require(tx, order.client_order_id)  # re-read in this tx
    mismatch = _identity_mismatch(current, snap)
    if mismatch is not None:
        return _halted(tx, current, HaltReason.RECON_ORDER, f"order identity mismatch: {mismatch}",
                       at)  # fmt: skip
    try:
        target = local_status_for(snap.broker_status, filled_qty=snap.filled_qty, qty=snap.qty)
    except UnmappedBrokerStatusError as exc:
        return _halted(tx, current, HaltReason.UNMAPPED_BROKER_STATUS, str(exc), at)
    except UnexpectedBrokerStatusError as exc:
        return _halted(tx, current, HaltReason.UNEXPECTED_BROKER_STATUS, str(exc), at)
    except BrokerStatusError as exc:
        return _halted(tx, current, HaltReason.BROKER_PAYLOAD, str(exc), at)

    if current.broker_order_id is None:
        OrdersRepository().set_broker_order_id(tx, current.client_order_id, snap.broker_order_id,
                                               at)  # fmt: skip
        record_event(tx, current.client_order_id, at=at, source=source, event="ADOPTED",
                     broker_order_id=snap.broker_order_id, broker_status=snap.broker_status,
                     was=current.status.value)  # fmt: skip
    if current.filled_qty > snap.filled_qty:
        return _halted(tx, current, HaltReason.RECON_ORDER,
                       f"booked fills {current.filled_qty} exceed broker filled_qty "
                       f"{snap.filled_qty}", at)  # fmt: skip
    if current.broker_filled_qty != snap.filled_qty:
        if current.broker_filled_qty is not None and snap.filled_qty < current.broker_filled_qty:
            return _halted(tx, current, HaltReason.RECON_ORDER,
                           f"broker filled_qty went backwards: {current.broker_filled_qty} -> "
                           f"{snap.filled_qty}", at)  # fmt: skip
        OrdersRepository().record_broker_progress(
            tx, current.client_order_id, broker_filled_qty=snap.filled_qty, at=at
        )
    awaiting = current.filled_qty < snap.filled_qty
    if awaiting and is_terminal(target):
        # Broker says terminal but some executions are not booked yet: stay working.
        target = (
            current.status
            if current.status not in AMBIGUOUS_STATUSES
            else (OrderStatus.PARTIALLY_FILLED if current.filled_qty else OrderStatus.ACCEPTED)
        )
    if target is current.status:
        return ApplyResult(
            ApplyOutcome.AWAITING_FILLS if awaiting else ApplyOutcome.UNCHANGED, current.status
        )
    try:
        step = transition(current.status, target)
    except IllegalTransitionError as exc:
        return _halted(tx, current, HaltReason.ILLEGAL_TRANSITION, str(exc), at)
    released = change_status(
        tx, current, target, portfolio=portfolio, at=at, source=source,
        event="BROKER_STATUS", broker_status=snap.broker_status, broker_filled_qty=snap.filled_qty,
        progress=OrderProgress(last_broker_status=snap.broker_status, last_synced_at=at),
    )  # fmt: skip
    if step.requires_halt:  # an order declared absent has appeared at the broker
        halt(tx, HaltReason.RECON_ORDER,
             f"order {current.client_order_id} was CONFIRMED_ABSENT but exists at the broker "
             f"({snap.broker_status})", at)  # fmt: skip
        return ApplyResult(ApplyOutcome.HALTED, target, released, HaltReason.RECON_ORDER)
    outcome = ApplyOutcome.AWAITING_FILLS if awaiting else ApplyOutcome.TRANSITIONED
    return ApplyResult(outcome, target, released)


def _halted(
    tx: Tx, order: OrderRecord, reason: HaltReason, detail: str, at: datetime
) -> ApplyResult:
    halt(tx, reason, f"{order.client_order_id}: {detail}", at)
    record_event(tx, order.client_order_id, at=at, source=EventSource.BROKER, event="HALT",
                 reason=reason.value, detail=detail)  # fmt: skip
    return ApplyResult(ApplyOutcome.HALTED, order.status, halt_reason=reason)


__all__ = [
    "OMS_ACTOR",
    "ApplyOutcome",
    "ApplyResult",
    "EventSource",
    "PersistenceError",
    "apply_snapshot",
    "change_status",
    "halt",
    "read_error_halt",
    "record_event",
]
