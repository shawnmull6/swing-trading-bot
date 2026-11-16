"""OrderManager: crash-safe entry submission and cancellation (ARCHITECTURE §6.4, §10, §12.4).

Submission is four short transactions around one broker call; no transaction is ever
open during the call (``assert_no_transaction``):

* **tx A — intent.** Inside ``BEGIN IMMEDIATE``: the ledger fingerprint must equal the
  one recorded in the :class:`RiskDecision`, the decision must be unexpired, and the
  risk engine is re-run on this exact snapshot with the approved quantity pinned. Only
  then are the ``PENDING_SUBMIT`` order (stable ``client_order_id``, UNIQUE intent key),
  its cash reservation and an audit event written — atomically.
* **tx A2 — send marker.** ``send_attempted_at`` (set-once) committed immediately
  before the network call: a NULL marker proves the order was never sent.
* **submit** — the only broker write. It is never retried.
* **tx B — outcome.** Acknowledged → adopted (``ACCEPTED`` or whatever the broker
  reports). Definitive rejection → ``REJECTED`` and the reservation is released.
  Duplicate client id → the order exists: ``SUBMIT_AMBIGUOUS`` and an immediate
  lookup/adoption. Anything else (429, 5xx, 504, timeout, reset, malformed response,
  auth failure) → ``SUBMIT_AMBIGUOUS`` with the reservation held; the recovery
  protocol (:mod:`swing_trading_bot.oms.recovery`) takes over.

Cancellation records ``PENDING_CANCEL`` first, then calls the broker. The reservation is
never released by a cancel request; only a broker-confirmed terminal status (applied
by synchronization once all fills are booked) releases the remainder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    DuplicateClientOrderIdError,
    NotCancelableError,
    OrderRejectedError,
)
from swing_trading_bot.broker.models import AccountSnapshot, SubmitOrderRequest
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.config.risk_limits import AmbiguousOrderPolicy
from swing_trading_bot.domain.enums import (
    HaltReason,
    IntentPurpose,
    OrderRole,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.domain.money import ZERO, MoneyError, valid_order_price, whole_shares
from swing_trading_bot.domain.state_machine import is_terminal
from swing_trading_bot.domain.trade_plan import (
    TradePlan,
    exit_intent_key,
    is_fingerprint,
    is_plan_id,
)
from swing_trading_bot.ledger.db import Database, Tx, assert_no_transaction
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.protection_store import GapReason, ProtectionGapsRepository
from swing_trading_bot.ledger.repositories import (
    OrderProgress,
    OrderRecord,
    OrdersRepository,
    PositionsRepository,
    PriceMarksRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.oms.execution import (
    ExecutionNotPermittedError,
    SimulationExecution,
    read_committed_cancel_marker,
    read_committed_send_marker,
)
from swing_trading_bot.oms.lifecycle import (
    EventSource,
    apply_snapshot,
    change_status,
    halt,
    record_event,
)
from swing_trading_bot.oms.protection import (
    persist_level,
    plan_owned_qty,
    protected_state,
    refresh_protection,
)
from swing_trading_bot.oms.share_reservations import SellSideBook
from swing_trading_bot.oms.trade_plans import (
    PLAN_RECORDED_EVENT,
    PlanIdentityError,
    load_plan,
    proposal_mismatch,
)
from swing_trading_bot.risk.gates import GateStatus, TradingGates
from swing_trading_bot.risk.pretrade import PreTradeRiskEngine, RiskDecision

CrashHook = Callable[[str], None]
"""Test hook called at each transaction boundary; raising or exiting simulates a crash."""

CRASH_POINTS: Final = (
    "after_intent_commit",  # tx A committed, nothing sent
    "send_marker_uncommitted",  # inside tx A2, before commit: the marker rolls back
    "after_send_marker",  # tx A2 committed, request not yet sent
    "after_submit",  # broker call returned (or raised), tx B not yet written
    "after_outcome_commit",  # tx B committed
    "after_cancel_marker",  # PENDING_CANCEL committed, cancel not yet sent
)


@dataclass(frozen=True, slots=True)
class OmsConfig:
    mode: RuntimeMode
    ambiguous: AmbiguousOrderPolicy
    posting_lag: timedelta = timedelta(minutes=15)
    activity_overlap: timedelta = timedelta(hours=1)
    lookup_interval: timedelta = timedelta(minutes=1)
    reconciliation_max_age: timedelta = timedelta(minutes=10)
    allow_unprotected_targets: bool = False
    """M2-3 safety (ARCHITECTURE §12.40): a TARGET sell while no protective stop can
    exist leaves the position unprotected (policy P1, proposed only). Refused unless an
    isolated offline OMS test opts in explicitly; no runtime path sets it."""

    @property
    def absent_window(self) -> timedelta:
        return timedelta(seconds=self.ambiguous.min_absent_window_seconds)

    @property
    def escalate_after(self) -> timedelta:
        return timedelta(seconds=self.ambiguous.escalate_after_seconds)

    @property
    def list_scan_lookback(self) -> timedelta:
        return timedelta(seconds=self.ambiguous.list_scan_lookback_seconds)


class PlacementOutcome(StrEnum):
    REFUSED = "REFUSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"


class RefusalCode(StrEnum):
    NOT_APPROVED = "NOT_APPROVED"
    DECISION_EXPIRED = "DECISION_EXPIRED"
    LEDGER_CHANGED = "LEDGER_CHANGED"
    REVALIDATION_FAILED = "REVALIDATION_FAILED"
    QUANTITY_CHANGED = "QUANTITY_CHANGED"
    RESERVATION_MISMATCH = "RESERVATION_MISMATCH"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
    # plan orders (M2-3)
    INVALID_PLAN_REQUEST = "INVALID_PLAN_REQUEST"
    PLAN_INTENT_MISMATCH = "PLAN_INTENT_MISMATCH"
    PLAN_IDENTITY_INVALID = "PLAN_IDENTITY_INVALID"
    STOP_ORDERS_UNSUPPORTED = "STOP_ORDERS_UNSUPPORTED"
    UNPROTECTED_TARGET = "UNPROTECTED_TARGET"
    ENTRY_STILL_WORKING = "ENTRY_STILL_WORKING"
    UNBOOKED_FILLS = "UNBOOKED_FILLS"
    NOTHING_TO_PROTECT = "NOTHING_TO_PROTECT"
    STOP_NOT_BELOW_MARK = "STOP_NOT_BELOW_MARK"
    MARK_UNAVAILABLE = "MARK_UNAVAILABLE"
    GATE_BLOCKED = "GATE_BLOCKED"
    HALTED = "HALTED"
    INTENT_KEY_USED = "INTENT_KEY_USED"
    WORKING_ORDER_EXISTS = "WORKING_ORDER_EXISTS"
    PLAN_UNKNOWN = "PLAN_UNKNOWN"
    PLAN_POSITION_MISMATCH = "PLAN_POSITION_MISMATCH"
    SHARES_UNAVAILABLE = "SHARES_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class PlacementResult:
    outcome: PlacementOutcome
    client_order_id: str | None
    status: OrderStatus | None = None
    refusal: RefusalCode | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PlanExitRequest:
    """A sell that belongs to a TradePlan (M2-3). ``generation`` makes the intent key
    deterministic: re-deriving the same step after a restart collides with the UNIQUE
    ``intent_key`` instead of creating a second order; a replacement uses the next one.
    ``plan_fingerprint`` must match the intent recorded with the plan's entry, so a sell
    can never be attributed to a plan under a different intent."""

    trade_plan_id: str
    role: OrderRole
    symbol: str
    qty: Decimal
    limit_price: Decimal
    generation: int
    plan_fingerprint: str
    time_in_force: TimeInForce = TimeInForce.DAY

    @classmethod
    def for_plan(
        cls, plan: TradePlan, role: OrderRole, *, qty: Decimal, limit_price: Decimal,
        generation: int, time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> PlanExitRequest:  # fmt: skip
        return cls(plan.plan_id, role, plan.intent.symbol, qty, limit_price, generation,
                   plan.fingerprint, time_in_force)  # fmt: skip

    @property
    def intent_key(self) -> str:
        return exit_intent_key(self.trade_plan_id, self.role, self.generation)

    @property
    def purpose(self) -> IntentPurpose:
        return (
            IntentPurpose.DISCRETIONARY_EXIT
            if self.role is OrderRole.TARGET
            else IntentPurpose.RISK_EXIT
        )


class CancelOutcome(StrEnum):
    REQUESTED = "REQUESTED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_CANCELABLE_LOCALLY = "NOT_CANCELABLE_LOCALLY"
    SEND_FAILED = "SEND_FAILED"


def check_execution_allowed(db: Database, execution: object) -> None:
    """M1 guard: execution exists only in simulate mode, and only through the explicit
    :class:`SimulationExecution` capability (SimBroker or a registered simulation
    double). Arbitrary execution implementations are refused."""
    if db.identity.mode is not RuntimeMode.SIMULATE:
        raise ExecutionNotPermittedError(
            f"order execution requires a simulate-mode ledger, not {db.identity.mode.value}"
        )
    if not isinstance(execution, SimulationExecution):
        raise ExecutionNotPermittedError(
            "the OrderManager executes only through SimulationExecution in M1"
        )


class OrderManager:
    def __init__(  # noqa: PLR0917 - explicit collaborators, all required
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        engine: PreTradeRiskEngine,
        gates: TradingGates,
        read: BrokerReadPort,
        execution: SimulationExecution,
        config: OmsConfig,
        *,
        crash: CrashHook | None = None,
        new_order_id: Callable[[], str] = new_client_order_id,
    ) -> None:
        check_execution_allowed(db, execution)
        if config.mode is not RuntimeMode.SIMULATE:
            raise ExecutionNotPermittedError("OmsConfig.mode must be SIMULATE in M1")
        self._db = db
        self._pf = portfolio
        self._engine = engine
        self._gates = gates
        self._read = read
        self._exec = execution
        self._config = config
        self._crash = crash or (lambda _point: None)
        self._new_id = new_order_id
        self._orders = OrdersRepository()

    @property
    def config(self) -> OmsConfig:
        return self._config

    # ============================================================== submission
    def place_entry(  # noqa: PLR0911 - each early return is a distinct refusal
        self, decision: RiskDecision, *, now: datetime, plan: TradePlan | None = None
    ) -> PlacementResult:
        """Create, reserve and submit the entry a :class:`RiskDecision` approved.

        With ``plan`` the order is that trade instance's ENTRY. The decision's proposal
        must be exactly the plan's entry (same intent key, symbol, prices, order shape),
        and the intent's canonical content and fingerprint are stored in the entry's
        append-only ``INTENT_CREATED`` event, which makes the plan durable.

        Retry semantics: if the plan's entry intent key is already used, nothing is
        created and the result is ``REFUSED``/``INTENT_KEY_USED`` carrying the existing
        order's ``client_order_id`` — a retry never creates a second trade. A new,
        independent trade must use a new :class:`TradePlan` (fresh ``plan_id``)."""
        assert_no_transaction("place_entry")
        if plan is not None:
            mismatch = proposal_mismatch(plan, decision.proposal)
            if mismatch is not None:
                return _refused(RefusalCode.PLAN_INTENT_MISMATCH, mismatch)
            with self._db.read() as s:
                existing = self._orders.get_by_intent_key(s, plan.entry_intent_key)
            if existing is not None:
                return _existing(existing)
        if not decision.approved:
            return _refused(RefusalCode.NOT_APPROVED, ",".join(decision.rejection_codes))
        if not now <= decision.valid_until or now < decision.evidence.evaluated_at:
            return _refused(RefusalCode.DECISION_EXPIRED, f"valid until {decision.valid_until}")
        # Fresh broker evidence and gate status, read OUTSIDE any transaction.
        try:
            account = self._read.get_account()
        except BrokerError as exc:
            return _refused(RefusalCode.ACCOUNT_UNAVAILABLE, type(exc).__name__)
        gate = self._gates.evaluate(now=now)

        coid = self._new_id()
        with self._db.transaction() as tx:
            refusal = self._create_intent(tx, decision, coid, account=account, gate=gate,
                                          now=now, plan=plan)  # fmt: skip
        if refusal is not None:
            return refusal
        self._crash("after_intent_commit")
        return self._send(coid, now=now)

    def _create_intent(
        self, tx: Tx, decision: RiskDecision, coid: str, *, account: AccountSnapshot,
        gate: GateStatus, now: datetime, plan: TradePlan | None = None,
    ) -> PlacementResult | None:  # fmt: skip
        if plan is not None:
            existing = self._orders.get_by_intent_key(tx, plan.entry_intent_key)
            if existing is not None:  # a concurrent retry committed first
                return _existing(existing)
        current = ledger_fingerprint(tx)
        if current != decision.evidence.ledger_fingerprint:
            return _refused(RefusalCode.LEDGER_CHANGED, "ledger state changed since the decision")
        p = replace(decision.proposal, requested_qty=decision.approved_qty)
        again = self._engine.evaluate_in(tx, p, gate=gate, account=account, now=now)
        if not again.approved:
            return _refused(RefusalCode.REVALIDATION_FAILED, ",".join(again.rejection_codes))
        if again.approved_qty != decision.approved_qty:  # pragma: no cover - qty is pinned
            return _refused(RefusalCode.QUANTITY_CHANGED, f"{again.approved_qty}")
        rec = OrderRecord(
            client_order_id=coid,
            portfolio_id=self._pf.portfolio(tx).id or 0,
            intent_key=p.intent_key,
            symbol=p.symbol,
            side=Side.BUY,
            order_type=p.order_type,
            time_in_force=p.time_in_force,
            purpose=IntentPurpose.ENTRY,
            qty=decision.approved_qty,
            limit_price=p.entry_price,
            planned_stop=p.stop_price,
            status=OrderStatus.PENDING_SUBMIT,
            filled_qty=ZERO,
            avg_fill_price=None,
            created_at=now,
            updated_at=now,
            trade_plan_id=None if plan is None else plan.plan_id,
            order_role=None if plan is None else OrderRole.ENTRY,
        )
        if plan is None:
            self._orders.insert(tx, rec)
        else:
            self._orders.insert_plan_order(tx, rec)
        fee = self._engine.fees.buy_fee(decision.approved_qty)
        reserved = self._pf.reserve_buy(tx, coid, fee_estimate=fee, now=now)
        if reserved != again.reservation:  # pragma: no cover - same formula both sides
            raise PersistenceError(f"reservation {reserved} != approved {again.reservation}")
        record_event(tx, coid, at=now, source=EventSource.LOCAL, event="INTENT_CREATED",
                     to_status=OrderStatus.PENDING_SUBMIT, decision_id=decision.decision_id,
                     revalidated_decision_id=again.decision_id, fingerprint=current,
                     qty=rec.qty, limit_price=rec.limit_price, planned_stop=rec.planned_stop,
                     reservation=reserved)  # fmt: skip
        if plan is not None:  # the durable copy of the trade instance's intent
            record_event(tx, coid, at=now, source=EventSource.LOCAL, event=PLAN_RECORDED_EVENT,
                         trade_plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint,
                         plan_intent=plan.intent.canonical())  # fmt: skip
        return None

    # ========================================================= plan exits (M2-3)
    def place_plan_exit(self, request: PlanExitRequest, *, now: datetime) -> PlacementResult:
        """Create and submit a sell that belongs to a TradePlan.

        Fail-closed, in this order: request shape; ``PROTECTIVE_STOP`` refused here (use
        :meth:`place_protective_stop`); ``TARGET`` refused unless an offline test opts in;
        the gate (a risk ``EXIT`` is refused only by all-order blockers, policy P3; a
        ``TARGET`` needs a fully clear gate); then, inside ``BEGIN IMMEDIATE``: no
        persistent halt, unused intent key,
        no other working order for the symbol (the M1 one-working-order rule, which the
        reconciler also enforces), plan-owned shares equal the ledger position, and the
        :class:`SellSideBook` permits the quantity. The order and its audit event commit
        together; submission then follows the same marker/outcome path as entries. Sells
        hold shares, not cash: no cash reservation is created.
        """
        assert_no_transaction("place_plan_exit")
        problem = _plan_exit_problem(request)
        if problem is not None:
            return _refused(*problem)
        if request.role is OrderRole.TARGET and not self._config.allow_unprotected_targets:
            return _refused(RefusalCode.UNPROTECTED_TARGET,
                            "no protective stop can exist yet (P1/P2 proposed only); TARGET "
                            "sells run only in isolated offline OMS tests")  # fmt: skip
        gate = self._gates.evaluate(now=now)
        # P3: a risk EXIT is protective, so entry-only blockers do not refuse it; a
        # TARGET (profit-taking) still needs a fully clear gate.
        blockers = (
            gate.blocking if request.role is OrderRole.TARGET else gate.protective_exit_blockers
        )
        if blockers:
            return _refused(RefusalCode.GATE_BLOCKED,
                            ",".join(sorted({f.reason.value for f in blockers})))  # fmt: skip
        coid = self._new_id()
        with self._db.transaction() as tx:
            refusal = self._create_plan_exit(tx, request, coid, now=now)
        if refusal is not None:
            return refusal
        self._crash("after_intent_commit")
        return self._send(coid, now=now)

    def _create_plan_exit(  # noqa: PLR0911 - each early return is a distinct refusal
        self, tx: Tx, req: PlanExitRequest, coid: str, *, now: datetime
    ) -> PlacementResult | None:
        if TradingStateRepository().get(tx).halted:
            return _refused(RefusalCode.HALTED, "persistent halt")
        existing = self._orders.get_by_intent_key(tx, req.intent_key)
        if existing is not None:
            return _existing(existing)
        pid = self._pf.portfolio(tx).id or 0
        working = [o for o in self._orders.list_working(tx) if o.symbol == req.symbol]
        if working:
            return _refused(RefusalCode.WORKING_ORDER_EXISTS,
                            ",".join(o.client_order_id for o in working))  # fmt: skip
        plan_orders = self._orders.list_for_plan(tx, req.trade_plan_id)
        if not any(o.order_role is OrderRole.ENTRY and o.symbol == req.symbol
                   for o in plan_orders):  # fmt: skip
            return _refused(RefusalCode.PLAN_UNKNOWN, req.trade_plan_id)
        try:
            stored = load_plan(tx, req.trade_plan_id)
        except PlanIdentityError as exc:
            return _refused(RefusalCode.PLAN_IDENTITY_INVALID, str(exc))
        if stored is None or stored.fingerprint != req.plan_fingerprint:
            return _refused(RefusalCode.PLAN_INTENT_MISMATCH,
                            "fingerprint differs from the plan's recorded intent")  # fmt: skip
        if stored.intent.symbol != req.symbol:
            return _refused(RefusalCode.PLAN_INTENT_MISMATCH, "symbol differs from the plan")
        pos = PositionsRepository().get(tx, pid, req.symbol)
        position_qty = ZERO if pos is None else pos.qty
        owned = sum((o.filled_qty if o.side is Side.BUY else -o.filled_qty
                     for o in plan_orders), ZERO)  # fmt: skip
        if owned != position_qty:
            return _refused(RefusalCode.PLAN_POSITION_MISMATCH,
                            f"plan owns {owned}, ledger position {position_qty}")  # fmt: skip
        book = SellSideBook.build(req.symbol, position_qty, working)
        refusals = book.check_new_sell(req.qty, trade_plan_id=req.trade_plan_id, role=req.role)
        if refusals:
            return _refused(RefusalCode.SHARES_UNAVAILABLE, ",".join(r.value for r in refusals))
        rec = OrderRecord(
            client_order_id=coid,
            portfolio_id=pid,
            intent_key=req.intent_key,
            symbol=req.symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            time_in_force=req.time_in_force,
            purpose=req.purpose,
            qty=req.qty,
            limit_price=req.limit_price,
            planned_stop=None,
            status=OrderStatus.PENDING_SUBMIT,
            filled_qty=ZERO,
            avg_fill_price=None,
            created_at=now,
            updated_at=now,
            trade_plan_id=req.trade_plan_id,
            order_role=req.role,
        )
        self._orders.insert_plan_order(tx, rec)
        record_event(tx, coid, at=now, source=EventSource.LOCAL, event="INTENT_CREATED",
                     to_status=OrderStatus.PENDING_SUBMIT, trade_plan_id=req.trade_plan_id,
                     role=req.role.value, qty=req.qty, limit_price=req.limit_price,
                     position_qty=position_qty, shares_reserved_before=book.reserved,
                     shares_available_before=book.available)  # fmt: skip
        return None

    # ===================================================== protective stops (16.12)
    def place_protective_stop(
        self, plan: TradePlan, *, generation: int, now: datetime
    ) -> PlacementResult:
        """Protect the plan's whole, fully booked position with one SELL STOP (GTC).

        Policies (approved for simulation, ARCHITECTURE §12.41): protection first (P1) —
        the stop covers every plan share, so no TARGET can coexist with it; durable stop
        price (P2); only all-order blockers refuse it (P3); small positions get the same
        whole-position stop (P4); a working entry must first be cancelled and its
        terminal status confirmed (P5); the stop level is the replayed level or the
        latest persisted one, whichever is higher (P6, P8).

        Fail-closed checks, in order: the gate's protective-exit blockers; then inside
        ``BEGIN IMMEDIATE``: no persistent halt, the plan's durable record verifies,
        unused intent key (an existing order is reported, nothing created), the entry is
        terminal and every plan fill booked, no other working order for the symbol,
        plan-owned shares equal the ledger position and are positive, a stored mark above
        the stop (a stop at or above the market would trigger at once), and the share
        book. The order, its audit event and any new stop level commit together, then
        submission follows the same committed-marker path as every order.
        """
        assert_no_transaction("place_protective_stop")
        if generation < 0:
            return _refused(RefusalCode.INVALID_PLAN_REQUEST, "generation must be >= 0")
        gate = self._gates.evaluate(now=now)
        blockers = gate.protective_exit_blockers
        if blockers:
            return _refused(RefusalCode.GATE_BLOCKED,
                            ",".join(sorted({f.reason.value for f in blockers})))  # fmt: skip
        coid = self._new_id()
        with self._db.transaction() as tx:
            refusal = self._create_protective_stop(tx, plan, coid, generation=generation,
                                                   now=now)  # fmt: skip
        if refusal is not None:
            return refusal
        self._crash("after_intent_commit")
        result = self._send(coid, now=now)
        self.refresh_protection(plan.plan_id, now=now)
        return result

    def _create_protective_stop(  # noqa: PLR0911, PLR0912 - one branch per distinct refusal
        self, tx: Tx, plan: TradePlan, coid: str, *, generation: int, now: datetime
    ) -> PlacementResult | None:
        if TradingStateRepository().get(tx).halted:
            return _refused(RefusalCode.HALTED, "persistent halt")
        try:
            stored = load_plan(tx, plan.plan_id)
        except PlanIdentityError as exc:
            return _refused(RefusalCode.PLAN_IDENTITY_INVALID, str(exc))
        if stored is None:
            return _refused(RefusalCode.PLAN_UNKNOWN, plan.plan_id)
        if stored.fingerprint != plan.fingerprint:
            return _refused(RefusalCode.PLAN_INTENT_MISMATCH, "plan recorded with another intent")
        key = exit_intent_key(plan.plan_id, OrderRole.PROTECTIVE_STOP, generation)
        existing = self._orders.get_by_intent_key(tx, key)
        if existing is not None:
            return _existing(existing)
        symbol = plan.intent.symbol
        plan_orders = self._orders.list_for_plan(tx, plan.plan_id)
        entry = next(o for o in plan_orders if o.order_role is OrderRole.ENTRY)
        if not is_terminal(entry.status):
            return _refused(RefusalCode.ENTRY_STILL_WORKING,
                            "cancel the entry's unfilled remainder and wait for the broker's "
                            "confirmation before protecting (P5)")  # fmt: skip
        if any(o.unbooked_fill_qty > ZERO for o in plan_orders):
            return _refused(RefusalCode.UNBOOKED_FILLS, "plan fills not yet booked")
        working = [o for o in self._orders.list_working(tx) if o.symbol == symbol]
        if working:
            return _refused(RefusalCode.WORKING_ORDER_EXISTS,
                            ",".join(o.client_order_id for o in working))  # fmt: skip
        pid = self._pf.portfolio(tx).id or 0
        pos = PositionsRepository().get(tx, pid, symbol)
        position_qty = ZERO if pos is None else pos.qty
        owned = plan_owned_qty(plan_orders)
        if owned != position_qty:
            return _refused(RefusalCode.PLAN_POSITION_MISMATCH,
                            f"plan owns {owned}, ledger position {position_qty}")  # fmt: skip
        if owned <= ZERO:
            return _refused(RefusalCode.NOTHING_TO_PROTECT, "no plan shares are held")
        state, stop = protected_state(tx, plan)
        if stop is None:  # pragma: no cover - a plan always has an initial stop
            return _refused(RefusalCode.NOTHING_TO_PROTECT, "no stop level")
        mark = PriceMarksRepository().get(tx, symbol)
        if mark is None:
            return _refused(RefusalCode.MARK_UNAVAILABLE, symbol)
        if mark.price <= stop:
            return _refused(RefusalCode.STOP_NOT_BELOW_MARK,
                            f"stop {stop} is not below the mark {mark.price}: use a risk "
                            "exit")  # fmt: skip
        book = SellSideBook.build(symbol, position_qty, working)
        refusals = book.check_new_sell(owned, trade_plan_id=plan.plan_id,
                                       role=OrderRole.PROTECTIVE_STOP)  # fmt: skip
        if refusals:
            return _refused(RefusalCode.SHARES_UNAVAILABLE, ",".join(r.value for r in refusals))
        rec = OrderRecord(
            client_order_id=coid,
            portfolio_id=pid,
            intent_key=key,
            symbol=symbol,
            side=Side.SELL,
            order_type=OrderType.STOP,
            time_in_force=TimeInForce.GTC,
            purpose=IntentPurpose.RISK_EXIT,
            qty=owned,
            limit_price=None,
            planned_stop=None,
            status=OrderStatus.PENDING_SUBMIT,
            filled_qty=ZERO,
            avg_fill_price=None,
            created_at=now,
            updated_at=now,
            trade_plan_id=plan.plan_id,
            order_role=OrderRole.PROTECTIVE_STOP,
            stop_price=stop,
        )
        self._orders.insert_plan_order(tx, rec)
        evidence = {"client_order_id": coid, "phase": state.phase.value,
                    "mark": str(mark.price), "mark_as_of": mark.as_of.isoformat()}  # fmt: skip
        persist_level(tx, plan, state, stop, evidence=evidence, at=now)
        record_event(tx, coid, at=now, source=EventSource.LOCAL, event="INTENT_CREATED",
                     to_status=OrderStatus.PENDING_SUBMIT, trade_plan_id=plan.plan_id,
                     role=OrderRole.PROTECTIVE_STOP.value, qty=owned, stop_price=stop,
                     phase=state.phase.value, position_qty=position_qty,
                     shares_reserved_before=book.reserved)  # fmt: skip
        return None

    def refresh_protection(self, plan_id: str, *, now: datetime) -> None:
        """Open or close the plan's unprotected interval from its orders (P7)."""
        assert_no_transaction("refresh_protection")
        with self._db.transaction() as tx:
            refresh_protection(tx, plan_id, at=now)

    def _send(self, coid: str, *, now: datetime) -> PlacementResult:
        with self._db.transaction() as tx:
            order = self._orders.require(tx, coid)
            self._orders.record_send_attempt(tx, coid, now)
            record_event(tx, coid, at=now, source=EventSource.LOCAL, event="SEND_ATTEMPT")
            self._crash("send_marker_uncommitted")  # dies inside tx A2: marker rolls back
        self._crash("after_send_marker")
        # The capability refuses to submit without this proof of a COMMITTED marker.
        marker = read_committed_send_marker(self._db, coid)
        request = SubmitOrderRequest(
            client_order_id=coid,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
        )
        assert_no_transaction("submit_order")
        snapshot: BrokerOrderSnapshot | None = None
        error: BaseException | None = None
        try:
            snapshot = self._exec.submit(request, marker=marker)
        except Exception as exc:  # noqa: BLE001 - every failure is classified below
            error = exc
        self._crash("after_submit")
        result = self._record_outcome(coid, snapshot, error, now=now)
        self._crash("after_outcome_commit")
        if isinstance(error, DuplicateClientOrderIdError):
            return self._adopt_duplicate(coid, now=now)
        return result

    def _record_outcome(
        self,
        coid: str,
        snapshot: BrokerOrderSnapshot | None,
        error: BaseException | None,
        *,
        now: datetime,
    ) -> PlacementResult:
        with self._db.transaction() as tx:
            order = self._orders.require(tx, coid)
            if snapshot is not None:
                applied = apply_snapshot(tx, order, snapshot, portfolio=self._pf, at=now,
                                         source=EventSource.BROKER)  # fmt: skip
                return PlacementResult(PlacementOutcome.ACCEPTED, coid, applied.status)
            if isinstance(error, OrderRejectedError):
                change_status(tx, order, OrderStatus.REJECTED, portfolio=self._pf, at=now,
                              source=EventSource.BROKER, event="SUBMIT_REJECTED",
                              progress=OrderProgress(reject_reason=str(error)[:500]),
                              status_code=error.status_code)  # fmt: skip
                return PlacementResult(PlacementOutcome.REJECTED, coid, OrderStatus.REJECTED,
                                       detail=str(error))  # fmt: skip
            kind = type(error).__name__ if error is not None else "NoResponse"
            code = getattr(error, "status_code", None)
            change_status(tx, order, OrderStatus.SUBMIT_AMBIGUOUS, portfolio=self._pf, at=now,
                          source=EventSource.BROKER, event="SUBMIT_AMBIGUOUS", error=kind,
                          status_code=code)  # fmt: skip
            if isinstance(error, AuthenticationError):
                halt(tx, HaltReason.AUTH, f"submit of {coid} failed authentication", now)
            return PlacementResult(PlacementOutcome.AMBIGUOUS, coid, OrderStatus.SUBMIT_AMBIGUOUS,
                                   detail=f"{kind} ({code})")  # fmt: skip

    def _adopt_duplicate(self, coid: str, *, now: datetime) -> PlacementResult:
        """The broker says the client id is taken: the order EXISTS. Look it up; never
        treat this as a rejection and never resend."""
        from swing_trading_bot.oms.recovery import AmbiguousOrderRecovery  # noqa: PLC0415

        recovery = AmbiguousOrderRecovery(self._db, self._pf, self._read, self._config)
        status = recovery.lookup(coid, now=now)
        return PlacementResult(
            PlacementOutcome.ACCEPTED if status not in (OrderStatus.SUBMIT_AMBIGUOUS,)
            else PlacementOutcome.AMBIGUOUS,
            coid, status, detail="duplicate client_order_id: order exists",
        )  # fmt: skip

    # ============================================================ cancellation
    def request_cancel(self, coid: str, *, now: datetime, reason: str = "") -> CancelOutcome:
        assert_no_transaction("request_cancel")
        with self._db.transaction() as tx:
            order = self._orders.require(tx, coid)
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                OrderStatus.REJECTED, OrderStatus.CONFIRMED_ABSENT,
                                OrderStatus.ORPHAN):  # fmt: skip
                return CancelOutcome.ALREADY_TERMINAL
            if order.broker_order_id is None or order.status not in (
                OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL
            ):  # fmt: skip
                # Ambiguous orders have no confirmed broker id: resolve them first.
                record_event(tx, coid, at=now, source=EventSource.LOCAL,
                             event="CANCEL_REFUSED_LOCALLY", status=order.status.value)  # fmt: skip
                return CancelOutcome.NOT_CANCELABLE_LOCALLY
            if order.status is not OrderStatus.PENDING_CANCEL:
                change_status(tx, order, OrderStatus.PENDING_CANCEL, portfolio=self._pf, at=now,
                              source=EventSource.LOCAL, event="CANCEL_REQUESTED",
                              reason=reason)  # fmt: skip
                if order.order_role is OrderRole.PROTECTIVE_STOP and order.trade_plan_id:
                    # P7: from this request on, the position may be unprotected.
                    ProtectionGapsRepository().open(
                        tx,
                        order.trade_plan_id,
                        reason=GapReason.STOP_CANCEL_REQUESTED,
                        prior=coid,
                        at=now,
                    )
        self._crash("after_cancel_marker")
        cancel_marker = read_committed_cancel_marker(self._db, coid)
        try:
            self._exec.cancel(marker=cancel_marker)
        except NotCancelableError as exc:
            self._note(coid, now, "CANCEL_NOT_CANCELABLE", error=str(exc))
            return CancelOutcome.ALREADY_TERMINAL
        except BrokerError as exc:
            self._note(coid, now, "CANCEL_SEND_FAILED", error=type(exc).__name__)
            return CancelOutcome.SEND_FAILED
        self._note(coid, now, "CANCEL_SENT")
        return CancelOutcome.REQUESTED

    def _note(self, coid: str, now: datetime, event: str, error: str | None = None) -> None:
        with self._db.transaction() as tx:
            record_event(tx, coid, at=now, source=EventSource.LOCAL, event=event, error=error)


def _plan_exit_problem(req: PlanExitRequest) -> tuple[RefusalCode, str] | None:
    if req.role is OrderRole.PROTECTIVE_STOP:
        return (RefusalCode.STOP_ORDERS_UNSUPPORTED,
                "protective stops are placed only by place_protective_stop")  # fmt: skip
    if req.role not in (OrderRole.TARGET, OrderRole.EXIT):
        return RefusalCode.INVALID_PLAN_REQUEST, f"role {req.role.value} is not a sell role"
    if (
        not is_plan_id(req.trade_plan_id)
        or not is_fingerprint(req.plan_fingerprint)
        or req.generation < 0
    ):
        return RefusalCode.INVALID_PLAN_REQUEST, "invalid plan id, fingerprint or generation"
    try:
        whole_shares(req.qty)
        valid_order_price(req.limit_price)
    except MoneyError as exc:
        return RefusalCode.INVALID_PLAN_REQUEST, str(exc)
    return None


def _existing(order: OrderRecord) -> PlacementResult:
    """The step already has an order: report it, create nothing (idempotent retry)."""
    return PlacementResult(PlacementOutcome.REFUSED, order.client_order_id, order.status,
                           RefusalCode.INTENT_KEY_USED, f"intent {order.intent_key} already "
                           f"has order {order.client_order_id} ({order.status.value})")  # fmt: skip


def _refused(code: RefusalCode, detail: str) -> PlacementResult:
    return PlacementResult(PlacementOutcome.REFUSED, None, refusal=code, detail=detail)


__all__ = [
    "CRASH_POINTS",
    "CancelOutcome",
    "CrashHook",
    "ExecutionNotPermittedError",
    "OmsConfig",
    "OrderManager",
    "PlacementOutcome",
    "PlacementResult",
    "PlanExitRequest",
    "RefusalCode",
    "check_execution_allowed",
]
