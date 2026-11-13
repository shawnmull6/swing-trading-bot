"""Broker-outage circuit breaker state (ARCHITECTURE §10.5, §12.21; M1 step 14.1).

A *failed cycle* is one broker synchronization cycle that could not complete because of
a transient broker failure (network, timeout, unavailable, 429, 5xx). The caller
classifies failures and reports at most one failure per cycle, identified by the
cycle's start time. This module keeps the active failure **streak** durably in
``sync_state`` under :data:`OUTAGE_STATE_KEY` (one JSON value, UTC timestamps; no schema
change) and decides escalation:

* the first failure starts a streak (counted cycle 1);
* a later failure is a new *counted* cycle only if at least ``min_cycle_spacing``
  (60 s) after the last counted cycle; otherwise it only updates ``last_failed_at``;
* a report for a cycle not later than ``last_failed_at`` is a duplicate and changes
  nothing (idempotent re-reporting);
* escalation to the persistent halt ``BROKER_OUTAGE`` needs **all** of: at least
  ``min_counted_cycles`` (3) counted cycles, and at least ``min_duration`` (30 min)
  between the first failure and the latest counted one. It happens once per streak:
  ``escalated_at`` is recorded in the same transaction as the halt, so re-checks never
  write a second halt event;
* a successful, complete cycle later than the last failure resets the streak (kept as
  ``previous`` for inspection). A reset never clears a halt.

Only counted cycles advance escalation, so a single failure followed by any amount of
silence never escalates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from swing_trading_bot.domain.enums import HaltReason
from swing_trading_bot.ledger.codec import ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.repositories import SyncStateRepository, TradingStateRepository

OUTAGE_STATE_KEY: Final = "broker.outage"
OUTAGE_ACTOR: Final = "outage_breaker"


@dataclass(frozen=True, slots=True)
class OutagePolicy:
    min_duration: timedelta = timedelta(minutes=30)
    min_counted_cycles: int = 3
    min_cycle_spacing: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.min_counted_cycles < 2 or self.min_cycle_spacing <= timedelta(0):
            raise ValueError("an outage needs at least 2 counted cycles and positive spacing")
        if self.min_duration < self.min_cycle_spacing:
            raise ValueError("min_duration must not be shorter than the cycle spacing")


class OutageEvent(StrEnum):
    STARTED = "STARTED"
    COUNTED = "COUNTED"
    TOO_SOON = "TOO_SOON"
    """A failure less than ``min_cycle_spacing`` after the last counted cycle."""
    DUPLICATE = "DUPLICATE"
    ESCALATED = "ESCALATED"
    RESET = "RESET"
    NO_STREAK = "NO_STREAK"
    UNCHANGED = "UNCHANGED"
    """Neither a qualifying failure nor a complete cycle (e.g. auth, identity, partial)."""


@dataclass(frozen=True, slots=True)
class OutageState:
    first_failed_at: datetime
    last_failed_at: datetime
    last_counted_at: datetime
    counted_cycles: int
    category: str
    categories: tuple[str, ...]
    escalated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_failed_at": ts_to_db(self.first_failed_at),
            "last_failed_at": ts_to_db(self.last_failed_at),
            "last_counted_at": ts_to_db(self.last_counted_at),
            "counted_cycles": self.counted_cycles,
            "category": self.category,
            "categories": list(self.categories),
            "escalated_at": None if self.escalated_at is None else ts_to_db(self.escalated_at),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OutageState:
        return cls(
            first_failed_at=ts_from_db(d["first_failed_at"]),
            last_failed_at=ts_from_db(d["last_failed_at"]),
            last_counted_at=ts_from_db(d["last_counted_at"]),
            counted_cycles=int(d["counted_cycles"]),
            category=str(d["category"]),
            categories=tuple(str(c) for c in d["categories"]),
            escalated_at=None if d.get("escalated_at") is None else ts_from_db(d["escalated_at"]),
        )

    def escalation_due(self, policy: OutagePolicy) -> bool:
        return (
            self.counted_cycles >= policy.min_counted_cycles
            and self.last_counted_at - self.first_failed_at >= policy.min_duration
        )


@dataclass(frozen=True, slots=True)
class OutageUpdate:
    event: OutageEvent
    state: OutageState | None
    escalated: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.value,
            "escalated_now": self.escalated,
            "streak": None if self.state is None else self.state.to_dict(),
        }


# ===================================================================== pure policy
def next_failure_state(
    state: OutageState | None, *, cycle_at: datetime, category: str, policy: OutagePolicy
) -> tuple[OutageEvent, OutageState]:
    """The streak after a failed cycle at ``cycle_at`` (no I/O, no escalation side effect)."""
    if state is None:
        return OutageEvent.STARTED, OutageState(
            cycle_at, cycle_at, cycle_at, 1, category, (category,)
        )
    if cycle_at <= state.last_failed_at:
        return OutageEvent.DUPLICATE, state
    categories = state.categories if category in state.categories else (
        *state.categories, category)  # fmt: skip
    if cycle_at - state.last_counted_at < policy.min_cycle_spacing:
        return OutageEvent.TOO_SOON, replace(
            state, last_failed_at=cycle_at, category=category, categories=categories
        )
    return OutageEvent.COUNTED, replace(
        state,
        last_failed_at=cycle_at,
        last_counted_at=cycle_at,
        counted_cycles=state.counted_cycles + 1,
        category=category,
        categories=categories,
    )


# ===================================================================== persistence
def _raw(s: Session) -> dict[str, Any] | None:
    """The stored state; an unreadable row fails closed (never silently "no outage")."""
    value = SyncStateRepository().get(s, OUTAGE_STATE_KEY)
    if value is None:
        return None
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict) or not isinstance(raw.get("active"), bool):
            raise TypeError("malformed")
        if raw["active"]:
            OutageState.from_dict(raw["streak"])
    except (ValueError, TypeError, KeyError):
        raise PersistenceError(f"{OUTAGE_STATE_KEY} state is unreadable") from None
    return dict(raw)


def load_streak(s: Session) -> OutageState | None:
    """The active failure streak, or None."""
    raw = _raw(s)
    if raw is None or raw.get("active") is not True:
        return None
    return OutageState.from_dict(raw["streak"])


def last_reset(s: Session) -> dict[str, Any] | None:
    raw = _raw(s)
    return None if raw is None or raw.get("active") else raw


def _store(tx: Tx, at: datetime, payload: dict[str, Any]) -> None:
    SyncStateRepository().set(tx, OUTAGE_STATE_KEY, json.dumps(payload, sort_keys=True), at)


def record_failed_cycle(
    tx: Tx, *, cycle_at: datetime, category: str, policy: OutagePolicy
) -> OutageUpdate:
    """Record one failed cycle and escalate once when the policy is met (same tx)."""
    current = load_streak(tx)
    event, state = next_failure_state(current, cycle_at=cycle_at, category=category,
                                      policy=policy)  # fmt: skip
    if event is OutageEvent.DUPLICATE:
        return OutageUpdate(event, state)
    escalated = False
    if state.escalated_at is None and state.escalation_due(policy):
        state = replace(state, escalated_at=cycle_at)
        detail = (
            f"broker unavailable: {state.counted_cycles} failed sync cycles from "
            f"{ts_to_db(state.first_failed_at)} to {ts_to_db(state.last_counted_at)} "
            f"({', '.join(state.categories)})"
        )
        repo = TradingStateRepository()
        prior = repo.get(tx)
        if prior.halted:
            detail += f" (previously halted: {prior.reason_code})"
        repo.halt(tx, HaltReason.BROKER_OUTAGE, detail=detail, actor=OUTAGE_ACTOR, at=cycle_at)
        escalated = True
        event = OutageEvent.ESCALATED
    _store(tx, cycle_at, {"active": True, "streak": state.to_dict()})
    return OutageUpdate(event, state, escalated)


def record_complete_cycle(tx: Tx, *, cycle_at: datetime) -> OutageUpdate:
    """A successful, complete cycle: reset the active streak (never clears a halt)."""
    current = load_streak(tx)
    if current is None:
        return OutageUpdate(OutageEvent.NO_STREAK, None)
    if cycle_at <= current.last_failed_at:
        return OutageUpdate(OutageEvent.DUPLICATE, current, notes=("older than the streak",))
    _store(tx, cycle_at, {"active": False, "reset_at": ts_to_db(cycle_at),
                          "previous": current.to_dict()})  # fmt: skip
    return OutageUpdate(OutageEvent.RESET, None)
