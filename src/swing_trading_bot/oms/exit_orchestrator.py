"""Exit orchestration for a TradePlan (M2-3, Step 16.13; ARCHITECTURE §12.43-§12.44).

Simulation only. :class:`ExitOrchestrator` coordinates the existing primitives — the
OrderManager (entry cancel, protective stops, plan exits, cancels), the derived share
book, protection gaps and status, persisted stop levels and the exit-state replay. It
adds no new execution path and decides nothing about entries or sizing: strategy
signals produce the :class:`TradePlan`, the risk engine sizes it, and this layer only
manages the exit of a booked position.

**One step, at most one action.** :meth:`ExitOrchestrator.step` reads one consistent
ledger snapshot, evaluates the gate and the caller's price evidence, and performs at
most one broker-facing action. Every decision is re-derived from durable state, so a
restart at any point simply resumes: nothing is remembered in memory, nothing is
resent, and an order of unknown fate blocks every further action until resolved.

**Protection first (P1).** The only state reported as protected is a broker-
acknowledged stop covering every held share. A stop that is pending, ambiguous or being
cancelled never counts. Under the one-working-order rule a resting partial-profit target
cannot coexist with the stop, so the integrated default never places one
(``PARTIAL_DEFERRED``). The *touch-triggered partial exit* — cancel the stop only when
validated evidence shows the price at or above the target, confirm, send a marketable
limit for the partial bucket with a short timeout, then re-protect — is a proposed
policy, simulation-only and **off by default** (it requires both
``ExitPolicy.touch_partial_exit`` and the OMS's ``allow_unprotected_targets``). Its
unprotected interval is recorded in ``protection_gaps``; it is **not approved for paper
execution** (Step 16.13A) and no application configuration can enable it.

**Breached stop (Step 16.13A).** When no stop works and the mark is already at or below
the stop level, no emergency order is sent. The step records the unprotected-position
evidence (``PROTECTION_BREACHED`` order event on the plan's entry) and raises the
persistent ``STOP_BREACHED_UNPROTECTED`` halt, which blocks every automated order —
entries included — survives restart, and clears only through the operator's guarded
resume (D11). Price recovery never clears it.

**Evidence contract (Step 16.13A).** :class:`PriceEvidence` must identify its symbol,
observation time, source and feed, trading session, a stable observation id and the
observed prices; see :func:`evidence_problems` and ``_context_problems``. Evidence that
fails any check is never persisted and never moves or triggers anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.enums import HaltReason, OrderRole, OrderStatus
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES, is_terminal
from swing_trading_bot.domain.trade_plan import ExitPhase, ExitState, TradePlan, TradePlanError
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database, Session, Tx, assert_no_transaction
from swing_trading_bot.ledger.portfolio import market_date
from swing_trading_bot.ledger.protection_store import ProtectionGapsRepository, StopLevel
from swing_trading_bot.ledger.repositories import (
    OrderEventsRepository,
    OrderRecord,
    OrdersRepository,
    PriceMarksRepository,
    TradingStateRepository,
)
from swing_trading_bot.oms.lifecycle import EventSource, record_event
from swing_trading_bot.oms.order_manager import (
    CancelOutcome,
    OrderManager,
    PlacementOutcome,
    PlacementResult,
    PlanExitRequest,
    RefusalCode,
)
from swing_trading_bot.oms.protection import (
    ProtectionState,
    observation_log,
    protected_state,
    protection_status,
    record_high_observation,
)
from swing_trading_bot.oms.trade_plans import require_plan
from swing_trading_bot.risk.gates import TradingGates

ORCHESTRATOR_ACTOR: Final = "exit-orchestrator"
CANCEL_FOR_PARTIAL: Final = "EXIT_ORCHESTRATOR:PARTIAL_TOUCH"
CANCEL_FOR_RAISE: Final = "EXIT_ORCHESTRATOR:RAISE_STOP"
CANCEL_ENTRY: Final = "EXIT_ORCHESTRATOR:ENTRY_REMAINDER"
CANCEL_PARTIAL_TIMEOUT: Final = "EXIT_ORCHESTRATOR:PARTIAL_TIMEOUT"
PROTECTION_BREACHED_EVENT: Final = "PROTECTION_BREACHED"
PROTECTION_FAILED_EVENT: Final = "PROTECTION_FAILED"
_OBSERVATION_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
_MAX_PRICE_PLACES: Final = 4


class ExitAction(StrEnum):
    """What one step did (or why it did nothing)."""

    INVALID_PLAN = "INVALID_PLAN"
    WAIT_ENTRY = "WAIT_ENTRY"
    CANCEL_ENTRY_REMAINDER = "CANCEL_ENTRY_REMAINDER"
    WAIT_RESOLUTION = "WAIT_RESOLUTION"
    """An order of unknown fate (pending/ambiguous submission) or unbooked fills."""
    WAIT_CANCEL = "WAIT_CANCEL"
    HOLD_BLOCKED = "HOLD_BLOCKED"
    """All-order restrictions apply: nothing is placed or cancelled."""
    PLACE_STOP = "PLACE_STOP"
    PROTECTED = "PROTECTED"
    """An acknowledged stop covers the position and nothing needs to change."""
    RECORD_HIGH = "RECORD_HIGH"
    PARTIAL_DEFERRED = "PARTIAL_DEFERRED"
    """The target was touched but no protection-preserving partial exit is enabled."""
    CANCEL_STOP_FOR_PARTIAL = "CANCEL_STOP_FOR_PARTIAL"
    PLACE_PARTIAL_EXIT = "PLACE_PARTIAL_EXIT"
    WAIT_PARTIAL = "WAIT_PARTIAL"
    CANCEL_PARTIAL_TIMEOUT = "CANCEL_PARTIAL_TIMEOUT"
    CANCEL_STOP_FOR_RAISE = "CANCEL_STOP_FOR_RAISE"
    WAIT_EXIT = "WAIT_EXIT"
    STOP_BREACHED_UNPROTECTED = "STOP_BREACHED_UNPROTECTED"
    """No stop works and the mark is at or below the stop level: a stop would trigger
    at once. No order is sent; the evidence is recorded and the persistent
    ``STOP_BREACHED_UNPROTECTED`` halt is raised (operator recovery only)."""
    HALT_PROTECTION_FAILED = "HALT_PROTECTION_FAILED"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    touch_partial_exit: bool = False
    """Proposed, simulation-only (§12.43). Off: the integrated workflow never sends a
    TARGET; a touched target is reported as ``PARTIAL_DEFERRED``."""
    partial_fill_timeout: timedelta = timedelta(minutes=2)
    """Bound on the unprotected interval of a touch-triggered partial exit."""
    min_stop_raise: Decimal = Decimal("0.05")
    """A live stop is replaced only when the durable level exceeds it by this much."""
    max_evidence_age: timedelta = timedelta(minutes=2)
    allowed_sources: frozenset[str] = frozenset({"SIM"})
    allowed_feeds: frozenset[str] = frozenset({"sim"})
    max_restore_attempts: int = 2
    """Consecutive rejected protective stops before halting ``PROTECTION_FAILED``."""

    def __post_init__(self) -> None:
        if self.partial_fill_timeout <= timedelta(0) or self.max_evidence_age <= timedelta(0):
            raise ValueError("timeouts must be positive")
        if self.min_stop_raise <= ZERO or self.max_restore_attempts < 1:
            raise ValueError("min_stop_raise must be positive and max_restore_attempts >= 1")
        if not self.allowed_sources or not self.allowed_feeds:
            raise ValueError("at least one evidence source and feed must be allowed")


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    """Market data supplied by the caller for one step: the latest regular-session
    trade and the session's high so far, with provenance. ``observation_id`` is the
    stable identity of the observation (the same observation always carries the same
    id); it makes re-delivery idempotent and ordering auditable."""

    symbol: str
    last: Decimal
    session_high: Decimal
    as_of: datetime
    source: str
    feed: str
    session: date
    observation_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "last": str(self.last),
            "high": str(self.session_high),
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "feed": self.feed,
            "session": self.session.isoformat(),
            "observation_id": self.observation_id,
        }


def _is(value: object, kind: type) -> bool:
    """Runtime type check of caller-supplied evidence (the annotations are not enforced)."""
    return isinstance(value, kind)


def _price_ok(x: object) -> bool:
    if not isinstance(x, Decimal) or not x.is_finite() or x <= ZERO:
        return False
    exp = x.as_tuple().exponent
    return isinstance(exp, int) and exp >= -_MAX_PRICE_PLACES


def evidence_problems(
    ev: PriceEvidence | None, *, symbol: str, now: datetime, policy: ExitPolicy
) -> tuple[str, ...]:
    """Why ``ev`` cannot be used, from the evidence alone (empty: valid so far; the
    step also applies the ledger checks of ``_context_problems``). Missing evidence is a
    problem, never a default."""
    if ev is None:
        return ("EVIDENCE_MISSING",)
    out: list[str] = []
    if not _is(ev.symbol, str) or ev.symbol != symbol:
        out.append("EVIDENCE_SYMBOL_MISMATCH")
    if not _is(ev.observation_id, str) or not _OBSERVATION_ID.match(ev.observation_id):
        out.append("EVIDENCE_IDENTITY_INVALID")
    if not (_price_ok(ev.last) and _price_ok(ev.session_high)) or ev.session_high < ev.last:
        out.append("EVIDENCE_PRICES_INVALID")
    if not _is(ev.as_of, datetime) or ev.as_of.utcoffset() is None:
        out.append("EVIDENCE_TIMESTAMP_NAIVE")
    elif type(ev.session) is not date:
        out.append("EVIDENCE_SESSION_MISMATCH")
    else:
        if ev.as_of > now:
            out.append("EVIDENCE_FROM_FUTURE")
        if now - ev.as_of > policy.max_evidence_age:
            out.append("EVIDENCE_STALE")
        if market_date(ev.as_of) != ev.session:
            out.append("EVIDENCE_SESSION_MISMATCH")
    if ev.source not in policy.allowed_sources:
        out.append("EVIDENCE_SOURCE_NOT_ALLOWED")
    if ev.feed not in policy.allowed_feeds:
        out.append("EVIDENCE_FEED_NOT_ALLOWED")
    return tuple(out)


def _context_problems(s: Session, plan_id: str, ev: PriceEvidence) -> tuple[str, ...]:
    """Ledger checks for evidence that passed :func:`evidence_problems`:

    * the session must be a regular session of the ledger's trading calendar and the
      observation inside its hours (``EVIDENCE_SESSION_UNKNOWN`` / ``_OUTSIDE_SESSION``);
    * an observation already applied to this plan is ``EVIDENCE_DUPLICATE`` (re-delivery
      is idempotent: it is neither re-persisted nor acted on again);
    * an observation not strictly newer than the latest applied one is
      ``EVIDENCE_OUT_OF_ORDER`` (late or replayed data never drives a decision).
    """
    out: list[str] = []
    session = CalendarStore().load(s).sessions.get(ev.session)
    if session is None:
        out.append("EVIDENCE_SESSION_UNKNOWN")
    elif not session.open_at <= ev.as_of < session.close_at:
        out.append("EVIDENCE_OUTSIDE_SESSION")
    log = observation_log(s, plan_id)
    if ev.observation_id in log.ids:
        out.append("EVIDENCE_DUPLICATE")
    elif log.latest_as_of is not None and ev.as_of <= log.latest_as_of:
        out.append("EVIDENCE_OUT_OF_ORDER")
    return tuple(out)


@dataclass(frozen=True, slots=True)
class StepResult:
    action: ExitAction
    protection: ProtectionState
    phase: ExitPhase | None
    detail: str = ""
    client_order_id: str | None = None
    placement: PlacementResult | None = None
    cancel: CancelOutcome | None = None
    level: StopLevel | None = None
    evidence_problems: tuple[str, ...] = field(default_factory=tuple)


class ExitOrchestrator:
    def __init__(
        self, db: Database, om: OrderManager, gates: TradingGates, policy: ExitPolicy
    ) -> None:
        if policy.touch_partial_exit and not om.config.allow_unprotected_targets:
            raise ValueError(
                "touch_partial_exit needs an OMS that allows TARGET sells (offline only)"
            )
        self._db = db
        self._om = om
        self._gates = gates
        self._policy = policy
        self._orders = OrdersRepository()

    # ------------------------------------------------------------------ step
    def step(  # noqa: PLR0911 - the decision ladder is the point of this method
        self, plan: TradePlan, *, now: datetime, evidence: PriceEvidence | None = None
    ) -> StepResult:
        assert_no_transaction("exit orchestration step")
        symbol = plan.intent.symbol
        problems = evidence_problems(evidence, symbol=symbol, now=now, policy=self._policy)
        try:
            with self._db.read() as s:
                require_plan(s, plan)
                orders = self._orders.list_for_plan(s, plan.plan_id)
                state, level = protected_state(s, plan)
                if evidence is not None and not problems:
                    problems = _context_problems(s, plan.plan_id, evidence)
        except TradePlanError as exc:
            return StepResult(
                ExitAction.INVALID_PLAN,
                ProtectionState.UNPROTECTED,
                None,
                str(exc),
                evidence_problems=problems,
            )

        valid = evidence if not problems else None
        ctx = _Ctx(plan, state.phase, problems)
        entry = next(o for o in orders if o.order_role is OrderRole.ENTRY)
        unresolved = [
            o
            for o in orders
            if o.status in AMBIGUOUS_STATUSES
            or o.status is OrderStatus.ORPHAN
            or o.unbooked_fill_qty > ZERO
        ]
        if unresolved:
            return self._res(
                ctx, ExitAction.WAIT_RESOLUTION, ",".join(o.client_order_id for o in unresolved)
            )
        blockers = self._gates.evaluate(now=now).protective_exit_blockers
        blocked = ",".join(sorted({f.reason.value for f in blockers}))

        # -------------------------------------------------------- entry (P5)
        if not is_terminal(entry.status):
            if entry.status is OrderStatus.PENDING_CANCEL:
                return self._res(ctx, ExitAction.WAIT_CANCEL, entry.client_order_id)
            if entry.filled_qty == ZERO:
                return self._res(ctx, ExitAction.WAIT_ENTRY, entry.client_order_id)
            if blockers:
                return self._res(ctx, ExitAction.HOLD_BLOCKED, blocked)
            out = self._om.request_cancel(entry.client_order_id, now=now, reason=CANCEL_ENTRY)
            return self._res(
                ctx,
                ExitAction.CANCEL_ENTRY_REMAINDER,
                entry.client_order_id,
                cancel=out,
                client_order_id=entry.client_order_id,
            )
        if state.phase in (ExitPhase.ENTRY_ABANDONED, ExitPhase.CLOSED) or state.held_qty <= ZERO:
            return self._res(ctx, ExitAction.DONE, state.phase.value)
        if blockers:  # never cancel or place anything; existing orders stay as they are
            return self._res(ctx, ExitAction.HOLD_BLOCKED, blocked)

        working = [o for o in orders if not is_terminal(o.status)]
        if working:
            return self._manage_working(ctx, working[-1], state, level, valid, now)
        return self._restore(ctx, orders, state, valid, now)

    # ------------------------------------------------------- working orders
    def _manage_working(  # noqa: PLR0911, PLR0917 - one return per decision
        self,
        ctx: _Ctx,
        w: OrderRecord,
        state: ExitState,
        level: Decimal | None,
        valid: PriceEvidence | None,
        now: datetime,
    ) -> StepResult:
        plan = ctx.plan
        if w.status is OrderStatus.PENDING_CANCEL:
            return self._res(ctx, ExitAction.WAIT_CANCEL, w.client_order_id)
        if w.order_role is OrderRole.TARGET:
            if now - w.created_at >= self._policy.partial_fill_timeout:
                out = self._om.request_cancel(
                    w.client_order_id, now=now, reason=CANCEL_PARTIAL_TIMEOUT
                )
                return self._res(
                    ctx,
                    ExitAction.CANCEL_PARTIAL_TIMEOUT,
                    w.client_order_id,
                    cancel=out,
                    client_order_id=w.client_order_id,
                )
            return self._res(ctx, ExitAction.WAIT_PARTIAL, w.client_order_id)
        if w.order_role is not OrderRole.PROTECTIVE_STOP:
            return self._res(ctx, ExitAction.WAIT_EXIT, w.client_order_id)
        # An acknowledged protective stop is working.
        recorded: StopLevel | None = None
        if valid is not None and state.phase is ExitPhase.RUNNER:
            with self._db.transaction() as tx:
                recorded = record_high_observation(
                    tx, plan, high=valid.session_high, evidence=valid.as_dict(), at=now
                )
            with self._db.read() as s:
                state, level = protected_state(s, plan)
        target_touched = (
            valid is not None
            and state.phase is ExitPhase.PROTECTED
            and state.partial_remaining > ZERO
            and valid.last >= plan.intent.partial_target
        )
        if target_touched:
            if not self._policy.touch_partial_exit:
                return self._res(
                    ctx,
                    ExitAction.PARTIAL_DEFERRED,
                    "target touched; no protected partial exit enabled",
                )
            out = self._om.request_cancel(w.client_order_id, now=now, reason=CANCEL_FOR_PARTIAL)
            return self._res(
                ctx,
                ExitAction.CANCEL_STOP_FOR_PARTIAL,
                w.client_order_id,
                cancel=out,
                client_order_id=w.client_order_id,
            )
        stop = w.stop_price
        if (
            valid is not None
            and level is not None
            and stop is not None
            and level >= stop + self._policy.min_stop_raise
        ):
            out = self._om.request_cancel(w.client_order_id, now=now, reason=CANCEL_FOR_RAISE)
            return self._res(
                ctx,
                ExitAction.CANCEL_STOP_FOR_RAISE,
                f"{stop} -> {level}",
                cancel=out,
                client_order_id=w.client_order_id,
            )
        if recorded is not None:
            return self._res(ctx, ExitAction.RECORD_HIGH, str(recorded.stop_price), level=recorded)
        return self._res(ctx, ExitAction.PROTECTED, w.client_order_id)

    # ---------------------------------------------- no working order: restore
    def _restore(
        self,
        ctx: _Ctx,
        orders: Sequence[OrderRecord],
        state: ExitState,
        valid: PriceEvidence | None,
        now: datetime,
    ) -> StepResult:
        plan = ctx.plan
        last = orders[-1]
        if (
            self._policy.touch_partial_exit
            and last.order_role is OrderRole.PROTECTIVE_STOP
            and last.status is OrderStatus.CANCELED
            and self._cancel_reason(last.client_order_id) == CANCEL_FOR_PARTIAL
            and state.phase is ExitPhase.PROTECTED
            and state.partial_remaining > ZERO
            and valid is not None
            and valid.last >= plan.intent.partial_target
        ):
            gen = sum(1 for o in orders if o.order_role is OrderRole.TARGET)
            req = PlanExitRequest.for_plan(
                plan,
                OrderRole.TARGET,
                qty=state.partial_remaining,
                limit_price=plan.intent.partial_target,
                generation=gen,
            )
            r = self._om.place_plan_exit(req, now=now)
            return self._res(
                ctx,
                ExitAction.PLACE_PARTIAL_EXIT,
                r.detail,
                placement=r,
                client_order_id=r.client_order_id,
            )
        stops = [o for o in orders if o.order_role is OrderRole.PROTECTIVE_STOP]
        rejected = 0
        for o in reversed(stops):
            if o.status is not OrderStatus.REJECTED:
                break
            rejected += 1
        if rejected >= self._policy.max_restore_attempts:
            self._halt(plan, rejected, now)
            return self._res(ctx, ExitAction.HALT_PROTECTION_FAILED, f"{rejected} rejected stops")
        r = self._om.place_protective_stop(plan, generation=len(stops), now=now)
        if r.refusal is RefusalCode.STOP_NOT_BELOW_MARK:
            detail = self._escalate_breach(ctx, orders, state, valid, r.detail, now)
            return self._res(ctx, ExitAction.STOP_BREACHED_UNPROTECTED, detail, placement=r)
        if r.outcome is PlacementOutcome.REJECTED and rejected + 1 >= (
            self._policy.max_restore_attempts
        ):
            self._halt(plan, rejected + 1, now)
            return self._res(
                ctx,
                ExitAction.HALT_PROTECTION_FAILED,
                r.detail,
                placement=r,
                client_order_id=r.client_order_id,
            )
        return self._res(
            ctx, ExitAction.PLACE_STOP, r.detail, placement=r, client_order_id=r.client_order_id
        )

    # --------------------------------------------------------------- helpers
    def _res(
        self,
        ctx: _Ctx,
        action: ExitAction,
        detail: str = "",
        *,
        client_order_id: str | None = None,
        placement: PlacementResult | None = None,
        cancel: CancelOutcome | None = None,
        level: StopLevel | None = None,
    ) -> StepResult:
        """The step's result, with the protection state re-read AFTER the action."""
        with self._db.read() as s:
            status = protection_status(s, ctx.plan.plan_id).state
        return StepResult(
            action,
            status,
            ctx.phase,
            detail,
            client_order_id,
            placement,
            cancel,
            level,
            ctx.problems,
        )

    def _cancel_reason(self, coid: str) -> str | None:
        with self._db.read() as s:
            events = OrderEventsRepository().list_for_order(s, coid)
        reasons = [
            str(e.detail.get("reason"))
            for e in events
            if e.detail and e.detail.get("event") == "CANCEL_REQUESTED"
        ]
        return reasons[-1] if reasons else None

    def _halt(self, plan: TradePlan, rejected: int, now: datetime) -> None:
        with self._db.transaction() as tx:
            orders = self._orders.list_for_plan(tx, plan.plan_id)
            entry = next(o for o in orders if o.order_role is OrderRole.ENTRY)
            stops = [o for o in orders if o.order_role is OrderRole.PROTECTIVE_STOP]
            record_event(tx, entry.client_order_id, at=now, source=EventSource.LOCAL,
                         event=PROTECTION_FAILED_EVENT, trade_plan_id=plan.plan_id,
                         rejected_stops=[o.client_order_id for o in stops[-rejected:]],
                         actor=ORCHESTRATOR_ACTOR)  # fmt: skip
            self._raise_halt(
                tx,
                HaltReason.PROTECTION_FAILED,
                f"plan {plan.plan_id}: {rejected} consecutive protective stops rejected; "
                "position unprotected",
                now,
            )

    def _escalate_breach(  # noqa: PLR0917 - the evidence of one decision
        self,
        ctx: _Ctx,
        orders: Sequence[OrderRecord],
        state: ExitState,
        valid: PriceEvidence | None,
        refusal: str,
        now: datetime,
    ) -> str:
        """Fail closed on a breached stop level with nothing working (Step 16.13A).

        One transaction: record the unprotected-position evidence on the plan's entry
        order (append-only ``order_events``) and raise the persistent
        ``STOP_BREACHED_UNPROTECTED`` halt. No order is sent. The halt blocks every
        automated order, survives restart and is never cleared here, whatever the price
        does next; only the operator's guarded resume clears it."""
        plan = ctx.plan
        symbol = plan.intent.symbol
        entry = next(o for o in orders if o.order_role is OrderRole.ENTRY)
        stops = [o for o in orders if o.order_role is OrderRole.PROTECTIVE_STOP]
        with self._db.transaction() as tx:
            _, level = protected_state(tx, plan)
            mark = PriceMarksRepository().get(tx, symbol)
            gap = ProtectionGapsRepository().open_gap(tx, plan.plan_id)
            evidence: dict[str, object] = {
                "trade_plan_id": plan.plan_id,
                "symbol": symbol,
                "held_qty": str(state.held_qty),
                "phase": state.phase.value,
                "stop_level": None if level is None else str(level),
                "mark": None if mark is None else {
                    "price": str(mark.price), "as_of": mark.as_of.isoformat(),
                    "received_at": mark.received_at.isoformat(),
                    "source": str(mark.source), "feed": mark.feed,
                },
                "last_protective_stop": None if not stops else {
                    "client_order_id": stops[-1].client_order_id,
                    "status": stops[-1].status.value,
                    "stop_price": None if stops[-1].stop_price is None
                    else str(stops[-1].stop_price),
                },
                "open_gap": None if gap is None else {
                    "id": gap.id, "opened_at": gap.opened_at.isoformat(),
                    "confirmed": gap.confirmed, "reason": gap.reason.value,
                },
                "step_evidence": None if valid is None else valid.as_dict(),
                "evidence_problems": list(ctx.problems),
                "refusal": refusal,
                "actor": ORCHESTRATOR_ACTOR,
            }  # fmt: skip
            record_event(tx, entry.client_order_id, at=now, source=EventSource.LOCAL,
                         event=PROTECTION_BREACHED_EVENT, evidence=evidence)  # fmt: skip
            price = "unknown" if mark is None else str(mark.price)
            detail = (
                f"plan {plan.plan_id}: {state.held_qty} {symbol} unprotected; stop level "
                f"{level} at or above mark {price}. No automatic exit was sent; operator "
                "recovery required"
            )
            self._raise_halt(tx, HaltReason.STOP_BREACHED_UNPROTECTED, detail, now)
        return detail

    @staticmethod
    def _raise_halt(tx: Tx, reason: HaltReason, detail: str, now: datetime) -> None:
        """Raise a persistent halt unless one is already in force (an earlier halt's
        reason is never overwritten; this step's evidence is recorded either way)."""
        repo = TradingStateRepository()
        if repo.get(tx).halted:
            return
        repo.halt(tx, reason, detail=detail[:2000], actor=ORCHESTRATOR_ACTOR, at=now)


@dataclass(frozen=True, slots=True)
class _Ctx:
    plan: TradePlan
    phase: ExitPhase
    problems: tuple[str, ...]


__all__ = [
    "CANCEL_ENTRY",
    "CANCEL_FOR_PARTIAL",
    "CANCEL_FOR_RAISE",
    "CANCEL_PARTIAL_TIMEOUT",
    "PROTECTION_BREACHED_EVENT",
    "PROTECTION_FAILED_EVENT",
    "ExitAction",
    "ExitOrchestrator",
    "ExitPolicy",
    "PriceEvidence",
    "StepResult",
    "evidence_problems",
]
