"""The M1 execution capability: simulation only, and only behind a committed marker.

:class:`SimulationExecution` is the **only** object the OrderManager accepts for broker
writes. It wraps exactly one of:

* a :class:`~swing_trading_bot.broker.sim.SimBroker` (exact type), or
* a class explicitly registered with :func:`register_simulation_double` — a controlled
  test double (e.g. the process-level crash harness's pipe proxy to a SimBroker). A
  double must declare a non-empty ``SIMULATION_DOUBLE_PURPOSE`` and must not live in a
  module whose name mentions Alpaca.

Anything else — an arbitrary object that happens to have ``submit_order``, a subclass of
SimBroker, the read-only Alpaca adapter (which has no ``submit_order`` at all) — is
refused at construction. There is no Alpaca execution implementation in M1.

Write ordering is enforced here, not by convention. :meth:`SimulationExecution.submit`
requires a :class:`CommittedSendMarker`, which can only be produced by
:func:`read_committed_send_marker`: it opens a *new* read transaction (so it can only
see committed data), asserts no transaction is open, and requires ``send_attempted_at``
to be set for that order. The broker therefore cannot be called for an order whose
send marker is not durably committed — and so a NULL marker after a crash is proof that
this process never sent the order. Cancellation likewise needs a
:class:`CommittedCancelMarker` (``PENDING_CANCEL`` committed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.ports import BrokerExecutionPort
from swing_trading_bot.broker.sim import SimBroker
from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.ledger.db import Database, assert_no_transaction
from swing_trading_bot.ledger.repositories import OrdersRepository


class ExecutionNotPermittedError(RuntimeError):
    """Execution was requested through something other than the M1 capability."""


class MarkerMissingError(ExecutionNotPermittedError):
    """A broker write was attempted without a committed marker."""


_REGISTERED_DOUBLES: set[type] = set()
_TOKEN: Final = object()


def register_simulation_double[T: type](cls: T) -> T:
    """Explicitly allow a controlled test double as a simulation execution target."""
    purpose = getattr(cls, "SIMULATION_DOUBLE_PURPOSE", "")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ExecutionNotPermittedError(f"{cls.__name__} must declare SIMULATION_DOUBLE_PURPOSE")
    if "alpaca" in cls.__module__.lower() or "alpaca" in cls.__name__.lower():
        raise ExecutionNotPermittedError("Alpaca classes can never be execution targets in M1")
    if not callable(getattr(cls, "submit_order", None)) or not callable(
        getattr(cls, "cancel_order", None)
    ):
        raise ExecutionNotPermittedError(f"{cls.__name__} does not implement the execution port")
    _REGISTERED_DOUBLES.add(cls)
    return cls


def is_permitted_target(target: object) -> bool:
    return type(target) is SimBroker or type(target) in _REGISTERED_DOUBLES


@dataclass(frozen=True, slots=True)
class CommittedSendMarker:
    client_order_id: str
    send_attempted_at: datetime
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MarkerMissingError("send markers come only from read_committed_send_marker")


@dataclass(frozen=True, slots=True)
class CommittedCancelMarker:
    client_order_id: str
    broker_order_id: str
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MarkerMissingError("cancel markers come only from read_committed_cancel_marker")


def read_committed_send_marker(db: Database, client_order_id: str) -> CommittedSendMarker:
    assert_no_transaction("reading the committed send marker")
    with db.read() as s:
        order = OrdersRepository().require(s, client_order_id)
    if order.send_attempted_at is None or order.status is not OrderStatus.PENDING_SUBMIT:
        raise MarkerMissingError(f"no committed send marker for {client_order_id} "
                                 f"(status {order.status.value})")  # fmt: skip
    return CommittedSendMarker(client_order_id, order.send_attempted_at, _TOKEN)


def read_committed_cancel_marker(db: Database, client_order_id: str) -> CommittedCancelMarker:
    assert_no_transaction("reading the committed cancel marker")
    with db.read() as s:
        order = OrdersRepository().require(s, client_order_id)
    if order.status is not OrderStatus.PENDING_CANCEL or order.broker_order_id is None:
        raise MarkerMissingError(f"no committed cancel request for {client_order_id}")
    return CommittedCancelMarker(client_order_id, order.broker_order_id, _TOKEN)


class SimulationExecution:
    """The M1 execution capability (see module docstring)."""

    def __init__(self, target: BrokerExecutionPort) -> None:
        if not is_permitted_target(target):
            raise ExecutionNotPermittedError(
                f"{type(target).__module__}.{type(target).__qualname__} is not a permitted "
                "M1 execution target (SimBroker or a registered simulation double)"
            )
        self._target = target

    def submit(
        self, request: SubmitOrderRequest, *, marker: CommittedSendMarker
    ) -> BrokerOrderSnapshot:
        if marker.client_order_id != request.client_order_id:
            raise MarkerMissingError("send marker belongs to a different order")
        assert_no_transaction("submit_order")
        return self._target.submit_order(request)

    def cancel(self, *, marker: CommittedCancelMarker) -> None:
        assert_no_transaction("cancel_order")
        self._target.cancel_order(marker.broker_order_id)


__all__ = [
    "CommittedCancelMarker",
    "CommittedSendMarker",
    "ExecutionNotPermittedError",
    "MarkerMissingError",
    "SimulationExecution",
    "is_permitted_target",
    "read_committed_cancel_marker",
    "read_committed_send_marker",
    "register_simulation_double",
]
