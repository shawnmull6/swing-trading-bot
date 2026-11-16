"""Bridges between the pure TradePlan contract and the existing risk engine and ledger
(M2-3, ARCHITECTURE §12.38-§12.39).

* :func:`entry_proposal` turns a :class:`TradePlan` into the existing
  :class:`TradeProposal`; position sizing stays entirely in the pre-trade risk engine.
* **Durable plan identity without a migration.** The plan's ENTRY order carries
  ``trade_plan_id`` and its append-only ``PLAN_RECORDED`` audit event (written in the same
  transaction as the order) stores the intent's
  canonical content and fingerprint. :func:`load_plan` rebuilds the :class:`TradePlan`
  from that copy after a restart and refuses anything that does not verify (missing,
  duplicated, or a fingerprint that does not match the stored content).
* :func:`plan_exit_events` rebuilds the exit-management event stream from booked fills
  of the plan's orders. Only broker-booked fills count; a fill reported but not yet
  booked is not an event.

Observed highs for trailing (``PRICE_HIGH``) are not stored in the ledger: the caller
supplies them. Persisting trailing state and its market-data evidence is part of the
proposed migration 0009 (ARCHITECTURE §12.39).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from swing_trading_bot.domain.enums import OrderRole, OrderType, Side
from swing_trading_bot.domain.state_machine import is_terminal
from swing_trading_bot.domain.trade_plan import (
    ExitEvent,
    ExitEventKind,
    ExitState,
    TradePlan,
    TradePlanError,
    TradePlanIntent,
    replay,
)
from swing_trading_bot.ledger.db import Session
from swing_trading_bot.ledger.repositories import (
    FillsRepository,
    OrderEventsRepository,
    OrdersRepository,
)
from swing_trading_bot.risk.pretrade import TradeProposal

PLAN_INTENT_KEY = "plan_intent"
PLAN_FINGERPRINT_KEY = "plan_fingerprint"
PLAN_RECORDED_EVENT = "PLAN_RECORDED"


class PlanIdentityError(TradePlanError):
    """The ledger's record of a plan is missing, ambiguous or does not verify."""


def entry_proposal(plan: TradePlan, *, requested_qty: Decimal | None = None) -> TradeProposal:
    """The risk-engine proposal for a plan's entry (limit buy at ``entry_limit`` with the
    plan's protective stop). ``requested_qty`` None asks for the maximum permitted."""
    intent = plan.intent
    return TradeProposal(
        intent_key=plan.entry_intent_key,
        symbol=intent.symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=intent.entry_time_in_force,
        entry_price=intent.entry_limit,
        stop_price=intent.protective_stop,
        instrument_type=intent.instrument_type,
        requested_qty=requested_qty,
    )


def proposal_mismatch(plan: TradePlan, proposal: TradeProposal) -> str | None:
    """Why ``proposal`` is not this plan's entry, or None when it is."""
    expected = entry_proposal(plan, requested_qty=proposal.requested_qty)
    fields = ("intent_key", "symbol", "side", "order_type", "time_in_force", "entry_price",
              "stop_price", "instrument_type", "extended_hours")  # fmt: skip
    bad = [f for f in fields if getattr(proposal, f) != getattr(expected, f)]
    return None if not bad else f"proposal differs from the plan's entry in: {', '.join(bad)}"


def load_plan(s: Session, plan_id: str) -> TradePlan | None:
    """The plan recorded in the ledger under ``plan_id``, or None if no entry exists.

    Raises :class:`PlanIdentityError` when the record is not exactly one ENTRY order with
    one verifying intent copy."""
    entries = [
        o for o in OrdersRepository().list_for_plan(s, plan_id) if o.order_role is OrderRole.ENTRY
    ]
    if not entries:
        return None
    if len(entries) > 1:
        raise PlanIdentityError(f"plan {plan_id} has {len(entries)} ENTRY orders")
    copies = [
        e.detail
        for e in OrderEventsRepository().list_for_order(s, entries[0].client_order_id)
        if e.detail and e.detail.get("event") == PLAN_RECORDED_EVENT
    ]
    if len(copies) != 1:
        raise PlanIdentityError(f"plan {plan_id}: expected one stored intent, found {len(copies)}")
    raw = copies[0][PLAN_INTENT_KEY]
    if not isinstance(raw, dict):
        raise PlanIdentityError(f"plan {plan_id}: stored intent is not an object")
    intent = TradePlanIntent.from_canonical(raw)
    if intent.fingerprint != copies[0].get(PLAN_FINGERPRINT_KEY):
        raise PlanIdentityError(f"plan {plan_id}: stored fingerprint does not match its intent")
    if intent.symbol != entries[0].symbol:
        raise PlanIdentityError(f"plan {plan_id}: stored intent symbol differs from its entry")
    return TradePlan(plan_id, intent)


def require_plan(s: Session, plan: TradePlan) -> None:
    """Fail unless the ledger's record of ``plan.plan_id`` is exactly ``plan``."""
    stored = load_plan(s, plan.plan_id)
    if stored is None:
        raise PlanIdentityError(f"plan {plan.plan_id} has no entry in the ledger")
    if stored.fingerprint != plan.fingerprint:
        raise PlanIdentityError(f"plan {plan.plan_id} was recorded with a different intent")


def plan_exit_events(s: Session, plan: TradePlan) -> list[ExitEvent]:
    """Ledger-derived events for ``plan``, in booked execution order, plus an
    ``ENTRY_CLOSED`` once the entry is terminal and fully booked."""
    orders = OrdersRepository().list_for_plan(s, plan.plan_id)
    fills_repo = FillsRepository()
    rows: list[tuple[datetime, int, ExitEvent]] = []
    entry_closed = False
    for o in orders:
        if o.symbol != plan.intent.symbol:
            raise PlanIdentityError(f"plan {plan.plan_id} has an order for {o.symbol}")
        if o.order_role is OrderRole.ENTRY:
            kind = ExitEventKind.ENTRY_FILL
            entry_closed = is_terminal(o.status) and o.unbooked_fill_qty == 0
        elif o.order_role is OrderRole.TARGET:
            kind = ExitEventKind.TARGET_FILL
        else:
            kind = ExitEventKind.STOP_FILL
        for f in fills_repo.list_for_order(s, o.client_order_id):
            rows.append(
                (f.filled_at, f.id or 0, ExitEvent(f.activity_id, kind, qty=f.qty, price=f.price))
            )
    rows.sort(key=lambda r: (r[0], r[1]))
    events = [ev for _, _, ev in rows]
    if entry_closed:
        # Exits are refused while the entry is working, so the entry closes before any
        # exit fill exists: insert the close right after the last entry fill.
        last_entry = max(
            (i for i, ev in enumerate(events) if ev.kind is ExitEventKind.ENTRY_FILL), default=-1
        )
        events.insert(
            last_entry + 1, ExitEvent(f"{plan.plan_id}:entry-closed", ExitEventKind.ENTRY_CLOSED)
        )
    return events


def rebuild_exit_state(
    s: Session, plan: TradePlan, highs: list[ExitEvent] | None = None
) -> ExitState:
    """Verify the plan against the ledger, then replay its booked fills and the
    caller-supplied ``PRICE_HIGH`` observations."""
    require_plan(s, plan)
    return replay(plan.intent, [*plan_exit_events(s, plan), *(highs or [])])


__all__ = [
    "PLAN_FINGERPRINT_KEY",
    "PLAN_INTENT_KEY",
    "PLAN_RECORDED_EVENT",
    "PlanIdentityError",
    "entry_proposal",
    "load_plan",
    "plan_exit_events",
    "proposal_mismatch",
    "rebuild_exit_state",
    "require_plan",
]
