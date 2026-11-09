"""Local order state machine (ARCHITECTURE §6.4, §10.3).

Every status change goes through :func:`transition`. Identity transitions are
allowed so that re-syncing the same broker state is idempotent. Any other change not
listed in ``_ALLOWED`` raises :class:`IllegalTransitionError`, which callers treat as
a persistent halt (``HaltReason.ILLEGAL_TRANSITION``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from swing_trading_bot.domain.enums import OrderStatus

S = OrderStatus

TERMINAL_STATUSES: Final = frozenset(
    {S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED, S.CONFIRMED_ABSENT, S.ORPHAN}
)
"""Statuses in which an order no longer holds a reservation or counts as working."""

WORKING_STATUSES: Final = frozenset(set(S) - TERMINAL_STATUSES)
"""Non-terminal statuses. An order in any of these counts toward the
one-working-order-per-symbol rule and keeps its reservation."""

AMBIGUOUS_STATUSES: Final = frozenset({S.PENDING_SUBMIT, S.SUBMIT_AMBIGUOUS})
"""Statuses whose broker-side existence is unknown; they block new orders."""

_BROKER_DERIVED: Final = frozenset(
    {
        S.ACCEPTED,
        S.PARTIALLY_FILLED,
        S.PENDING_CANCEL,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.REJECTED,
    }
)

_ALLOWED: Final[Mapping[OrderStatus, frozenset[OrderStatus]]] = {
    # Before/around submission, the broker may report any state on adoption.
    S.PENDING_SUBMIT: _BROKER_DERIVED | {S.SUBMIT_AMBIGUOUS},
    # Absence can only be declared from the ambiguous state, by an operator (U4).
    S.SUBMIT_AMBIGUOUS: _BROKER_DERIVED | {S.CONFIRMED_ABSENT},
    S.ACCEPTED: frozenset(
        {S.PARTIALLY_FILLED, S.PENDING_CANCEL, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED}
    ),
    S.PARTIALLY_FILLED: frozenset({S.PENDING_CANCEL, S.FILLED, S.CANCELED, S.EXPIRED}),
    # A cancel request can be overtaken by fills or refused by the broker.
    S.PENDING_CANCEL: frozenset({S.ACCEPTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED}),
    # Late appearance of an order declared absent: adopt it, but callers must halt.
    S.CONFIRMED_ABSENT: _BROKER_DERIVED,
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.EXPIRED: frozenset(),
    S.REJECTED: frozenset(),
    S.ORPHAN: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"illegal order transition {current} -> {target}")
        self.current = current
        self.target = target


@dataclass(frozen=True, slots=True)
class Transition:
    current: OrderStatus
    target: OrderStatus
    requires_halt: bool
    """True when the transition is legal but signals a reconciliation failure."""

    @property
    def is_noop(self) -> bool:
        return self.current is self.target

    @property
    def releases_reservation(self) -> bool:
        return self.current not in TERMINAL_STATUSES and self.target in TERMINAL_STATUSES


def allowed_targets(current: OrderStatus) -> frozenset[OrderStatus]:
    return _ALLOWED[current]


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


def transition(current: OrderStatus, target: OrderStatus) -> Transition:
    if current is target:
        return Transition(current, target, requires_halt=False)
    if target not in _ALLOWED[current]:
        raise IllegalTransitionError(current, target)
    return Transition(current, target, requires_halt=current is S.CONFIRMED_ABSENT)
