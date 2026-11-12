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
    OrderStatus,
    RuntimeMode,
    Side,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.db import Database, Tx, assert_no_transaction
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrderProgress,
    OrderRecord,
    OrdersRepository,
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


@dataclass(frozen=True, slots=True)
class PlacementResult:
    outcome: PlacementOutcome
    client_order_id: str | None
    status: OrderStatus | None = None
    refusal: RefusalCode | None = None
    detail: str = ""


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

    # ============================================================== submission
    def place_entry(self, decision: RiskDecision, *, now: datetime) -> PlacementResult:
        """Create, reserve and submit the entry a :class:`RiskDecision` approved."""
        assert_no_transaction("place_entry")
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
                                          now=now)  # fmt: skip
        if refusal is not None:
            return refusal
        self._crash("after_intent_commit")
        return self._send(coid, now=now)

    def _create_intent(
        self, tx: Tx, decision: RiskDecision, coid: str, *, account: AccountSnapshot,
        gate: GateStatus, now: datetime,
    ) -> PlacementResult | None:  # fmt: skip
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
        )
        self._orders.insert(tx, rec)
        fee = self._engine.fees.buy_fee(decision.approved_qty)
        reserved = self._pf.reserve_buy(tx, coid, fee_estimate=fee, now=now)
        if reserved != again.reservation:  # pragma: no cover - same formula both sides
            raise PersistenceError(f"reservation {reserved} != approved {again.reservation}")
        record_event(tx, coid, at=now, source=EventSource.LOCAL, event="INTENT_CREATED",
                     to_status=OrderStatus.PENDING_SUBMIT, decision_id=decision.decision_id,
                     revalidated_decision_id=again.decision_id, fingerprint=current,
                     qty=rec.qty, limit_price=rec.limit_price, planned_stop=rec.planned_stop,
                     reservation=reserved)  # fmt: skip
        return None

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
    "RefusalCode",
    "check_execution_allowed",
]
