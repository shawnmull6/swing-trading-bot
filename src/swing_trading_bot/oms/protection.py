"""Protective-stop state of a TradePlan (M2-3, Step 16.12; ARCHITECTURE §12.41).

Pure ledger logic, no broker calls; every function runs inside the caller's transaction.

* **Stop level (P6, P8).** The protective stop of a plan is the higher of the level the
  exit-state replay derives (initial stop; breakeven after the whole partial bucket;
  trailing ratchet over observed highs) and the latest persisted ``plan_stop_levels``
  row. Persisted rows carry their evidence and only ratchet up, so a restart can never
  lower a stop.
* **Protection gaps (P7).** :func:`refresh_protection` keeps ``protection_gaps`` in line
  with the plan's orders: a gap opened by a protective-stop cancel request (recorded by
  the OrderManager in the same transaction as ``PENDING_CANCEL``) closes once another
  protective stop is acknowledged by the broker, or the position is flat; a protective
  stop that ended without filling while shares remain opens a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from swing_trading_bot.domain.enums import OrderRole, OrderStatus, Side
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES, is_terminal
from swing_trading_bot.domain.trade_plan import (
    ExitEvent,
    ExitEventKind,
    ExitPhase,
    ExitState,
    TradePlan,
)
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.protection_store import (
    GapCloseReason,
    GapReason,
    PlanStopLevelsRepository,
    ProtectionGap,
    ProtectionGapsRepository,
    StopLevel,
    StopLevelSource,
)
from swing_trading_bot.ledger.repositories import OrderRecord, OrdersRepository
from swing_trading_bot.oms.trade_plans import rebuild_exit_state

_ACKNOWLEDGED = frozenset({OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED})


def persisted_highs(s: Session, plan: TradePlan) -> list[ExitEvent]:
    """Persisted high-water observations as idempotent ``PRICE_HIGH`` events."""
    return [
        ExitEvent(f"level:{plan.plan_id}:{lv.seq}", ExitEventKind.PRICE_HIGH, price=lv.high_water)
        for lv in PlanStopLevelsRepository().list_for_plan(s, plan.plan_id)
        if lv.high_water is not None
    ]


def protected_state(s: Session, plan: TradePlan) -> tuple[ExitState, Decimal | None]:
    """The replayed exit state (fills + persisted highs) and the effective stop: the
    higher of the replayed stop and the latest persisted level."""
    state = rebuild_exit_state(s, plan, persisted_highs(s, plan))
    latest = PlanStopLevelsRepository().latest(s, plan.plan_id)
    stops = [x for x in (state.stop_price, None if latest is None else latest.stop_price) if x]
    return state, (max(stops) if stops else None)


def _source(state: ExitState) -> StopLevelSource:
    if state.phase is ExitPhase.RUNNER:
        return (
            StopLevelSource.TRAILING
            if state.high_water is not None
            else (StopLevelSource.BREAKEVEN)
        )
    return StopLevelSource.PLAN_INTENT


def persist_level(
    tx: Tx, plan: TradePlan, state: ExitState, stop: Decimal, *, evidence: dict[str, Any],
    at: datetime,
) -> StopLevel | None:  # fmt: skip
    """Append ``stop`` when it is new or higher than the latest persisted level."""
    repo = PlanStopLevelsRepository()
    latest = repo.latest(tx, plan.plan_id)
    high = state.high_water
    if latest is not None and latest.high_water is not None:
        high = latest.high_water if high is None else max(high, latest.high_water)
    if latest is not None and stop <= latest.stop_price and high == latest.high_water:
        return None
    source = _source(state)
    if source is StopLevelSource.TRAILING and high is None:  # pragma: no cover - guarded
        source = StopLevelSource.BREAKEVEN
    final = stop if latest is None else max(stop, latest.stop_price)
    return repo.append(tx, plan.plan_id, stop_price=final, high_water=high, source=source,
                       evidence=evidence, at=at)  # fmt: skip


def record_high_observation(
    tx: Tx, plan: TradePlan, *, high: Decimal, evidence: dict[str, Any], at: datetime
) -> StopLevel | None:
    """Persist an observed high (with its market-data evidence, e.g. the daily bar's
    session, feed and timestamp) and the stop it implies. Never lowers anything. The
    evidence is required: a high without provenance is not recorded."""
    if not evidence:
        raise ValueError("a high observation needs its market-data evidence")
    state, stop = protected_state(tx, plan)
    obs = ExitEvent(f"obs:{plan.plan_id}:{at.isoformat()}:{high}", ExitEventKind.PRICE_HIGH,
                    price=high)  # fmt: skip
    state = state.apply(obs)
    candidates = [x for x in (state.stop_price, stop) if x is not None]
    if not candidates:
        return None
    return persist_level(tx, plan, state, max(candidates), evidence=evidence, at=at)


def plan_owned_qty(orders: list[OrderRecord]) -> Decimal:
    return sum((o.filled_qty if o.side is Side.BUY else -o.filled_qty for o in orders), ZERO)


def _covering(stops: list[OrderRecord], owned: Decimal) -> OrderRecord | None:
    """The latest protective stop the broker has acknowledged (``ACCEPTED`` or
    ``PARTIALLY_FILLED``, with a broker id) whose unfilled quantity covers ``owned``. A
    stop in ``PENDING_CANCEL``, ``PENDING_SUBMIT`` or ``SUBMIT_AMBIGUOUS`` is never
    counted as protection: a completed send attempt is not an acknowledgement."""
    live = [
        o for o in stops
        if o.status in _ACKNOWLEDGED and o.broker_order_id is not None
        and o.qty - o.filled_qty >= owned
    ]  # fmt: skip
    return live[-1] if live else None


def refresh_protection(tx: Tx, plan_id: str, *, at: datetime) -> None:
    """Bring the plan's protection gap in line with its orders (P7). Idempotent.

    * Open gap, position flat → closed ``POSITION_FLAT``.
    * Open gap, an acknowledged stop covers the shares → closed ``STOP_ACKNOWLEDGED``
      (a declined cancel closes on the same stop, never having been confirmed).
    * Open potential gap whose prior stop is broker-confirmed terminal with every fill
      booked → **confirmed** at the later of the gap's opening and that order's last
      update. An ambiguous or merely submitted replacement does not prevent this.
    * No gap, shares held, no working stop, and the latest stop ended unfilled → a gap
      opens already confirmed (``STOP_ENDED_UNFILLED``).
    """
    gaps = ProtectionGapsRepository()
    orders = OrdersRepository().list_for_plan(tx, plan_id)
    stops = [o for o in orders if o.order_role is OrderRole.PROTECTIVE_STOP]
    owned = plan_owned_qty(orders)
    covering = _covering(stops, owned)
    gap = gaps.open_gap(tx, plan_id)
    if gap is not None:
        if owned <= ZERO:
            gaps.close(tx, gap.id, reason=GapCloseReason.POSITION_FLAT, by=None, at=at)
        elif covering is not None:
            gaps.close(tx, gap.id, reason=GapCloseReason.STOP_ACKNOWLEDGED,
                       by=covering.client_order_id, at=at)  # fmt: skip
        elif not gap.confirmed:
            prior = next(o for o in stops if o.client_order_id == gap.prior_client_order_id)
            if is_terminal(prior.status) and prior.unbooked_fill_qty == ZERO:
                gaps.confirm(tx, gap.id, at=max(gap.opened_at, min(prior.updated_at, at)))
        return
    working = [o for o in stops if not is_terminal(o.status)]
    if owned > ZERO and not working and stops and stops[-1].status is not OrderStatus.FILLED:
        gaps.open(tx, plan_id, reason=GapReason.STOP_ENDED_UNFILLED,
                  prior=stops[-1].client_order_id, at=at)  # fmt: skip


class ProtectionState(StrEnum):
    FLAT = "FLAT"
    PROTECTED = "PROTECTED"
    """An acknowledged stop covers every held share."""
    POTENTIAL_GAP = "POTENTIAL_GAP"
    """A stop is being canceled: it may or may not still protect the shares."""
    UNCERTAIN = "UNCERTAIN"
    """The only stop's broker existence is unknown (pending or ambiguous submission)."""
    UNPROTECTED = "UNPROTECTED"
    """Shares are held and no stop is working (never placed, or confirmed gone)."""


@dataclass(frozen=True, slots=True)
class ProtectionStatus:
    state: ProtectionState
    held_qty: Decimal
    stop: OrderRecord | None
    open_gap: ProtectionGap | None


def protection_status(s: Session, plan_id: str) -> ProtectionStatus:
    """Read-only protection state of a plan, derived from durable orders and gaps."""
    orders = OrdersRepository().list_for_plan(s, plan_id)
    stops = [o for o in orders if o.order_role is OrderRole.PROTECTIVE_STOP]
    owned = plan_owned_qty(orders)
    gap = ProtectionGapsRepository().open_gap(s, plan_id)
    if owned <= ZERO:
        return ProtectionStatus(ProtectionState.FLAT, owned, None, gap)
    covering = _covering(stops, owned)
    if covering is not None:
        return ProtectionStatus(ProtectionState.PROTECTED, owned, covering, gap)
    working = [o for o in stops if not is_terminal(o.status)]
    if any(o.status is OrderStatus.PENDING_CANCEL for o in working):
        state = ProtectionState.POTENTIAL_GAP
    elif any(o.status in AMBIGUOUS_STATUSES for o in working):
        state = ProtectionState.UNCERTAIN
    else:
        state = ProtectionState.UNPROTECTED
    return ProtectionStatus(state, owned, working[-1] if working else None, gap)


__all__ = [
    "ProtectionState",
    "ProtectionStatus",
    "persist_level",
    "persisted_highs",
    "plan_owned_qty",
    "protected_state",
    "protection_status",
    "record_high_observation",
    "refresh_protection",
]
