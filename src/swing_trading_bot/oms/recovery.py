"""Ambiguous-order recovery protocol and restart recovery (ARCHITECTURE §10.1, §10.3).

Applies to orders in ``PENDING_SUBMIT`` (crash before/around the send) and
``SUBMIT_AMBIGUOUS`` (timeout, reset, 429, 5xx, 504, malformed or missing response).

* The reservation stays ACTIVE and gates block new entries while any exists.
* **Never resent.** The automated path never submits again, under any client id.
* Lookup by client id (tx F). Found → adopted through the normal lifecycle (fills,
  fill-gated terminal status). Not found — including an explicit 404 — is recorded as
  evidence and proves nothing on its own.
* ``CONFIRMED_ABSENT`` exists only through :meth:`confirm_absent`, an operator action
  that refuses unless ALL evidence is present (U4):

  - at least ``min_absent_lookups`` not-found lookups after the last possible send,
    the latest at least ``min_absent_window`` after it, and no lookup that found it;
  - a full order-list scan from ``created_at - list_scan_lookback`` without it, taken
    after that window;
  - a complete (tail-confirmed, closed) activity scan from the same start, whose
    closed end reaches past that window, with no fill that cannot be attributed to a
    known order;
  - a clean (``OK``) reconciliation run recorded after all of the above.

* Unresolved longer than ``escalate_after`` (30 min) since the last possible send →
  persistent halt ``AMBIGUOUS_ORDER_TIMEOUT`` (once per order).
* An order declared absent that later appears is adopted and halts ``RECON_ORDER``
  (:func:`swing_trading_bot.oms.lifecycle.apply_snapshot`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from swing_trading_bot.broker.activity_feed import collect_window
from swing_trading_bot.broker.errors import (
    BrokerError,
    IncompleteRetrievalError,
    OrderNotFoundError,
)
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.domain.enums import HaltReason, OrderStatus, ReconciliationResult
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES
from swing_trading_bot.ledger.db import Database, Session, Tx, assert_no_transaction
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrderRecord,
    OrdersRepository,
    ReconciliationRunsRepository,
    ResolutionAttempt,
    ResolutionAttemptsRepository,
    ResolutionMethod,
    TradingStateRepository,
)
from swing_trading_bot.oms.lifecycle import (
    EventSource,
    apply_snapshot,
    change_status,
    halt,
    read_error_halt,
    record_event,
)
from swing_trading_bot.oms.order_manager import OmsConfig
from swing_trading_bot.oms.sync import OrderSync, SyncReport


class AttemptResult(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
    CLEAN = "CLEAN"
    UNATTRIBUTED_FILLS = "UNATTRIBUTED_FILLS"
    INCOMPLETE = "INCOMPLETE"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class AbsenceEvidence:
    client_order_id: str
    last_possible_send: datetime
    not_found_lookups: int
    latest_lookup: datetime | None
    list_scan_clean_at: datetime | None
    activity_scan_clean_at: datetime | None
    reconciliation_ok_at: datetime | None
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        def iso(v: datetime | None) -> str | None:
            return None if v is None else v.isoformat()

        return {
            "last_possible_send": self.last_possible_send.isoformat(),
            "not_found_lookups": self.not_found_lookups,
            "latest_lookup": iso(self.latest_lookup),
            "list_scan_clean_at": iso(self.list_scan_clean_at),
            "activity_scan_clean_at": iso(self.activity_scan_clean_at),
            "reconciliation_ok_at": iso(self.reconciliation_ok_at),
            "missing": list(self.missing),
        }


@dataclass(slots=True)
class RecoveryReport:
    looked_up: dict[str, OrderStatus] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)


def last_possible_send(order: OrderRecord) -> datetime:
    """The latest moment the broker could have received this order: the send marker if
    one was written, else creation (an unmarked order was never sent)."""
    return order.send_attempted_at or order.created_at


class AmbiguousOrderRecovery:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        read: BrokerReadPort,
        config: OmsConfig,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._read = read
        self._config = config
        self._orders = OrdersRepository()
        self._attempts = ResolutionAttemptsRepository()

    # ----------------------------------------------------------- lookups (tx F)
    def lookup(self, coid: str, *, now: datetime) -> OrderStatus:
        """One lookup by client id. Never resends; never infers absence."""
        assert_no_transaction("ambiguous-order lookup")
        snap: BrokerOrderSnapshot | None = None
        error: BrokerError | None = None
        try:
            snap = self._read.get_order_by_client_id(coid)
        except OrderNotFoundError:
            snap = None  # a 404 is not proof of non-acceptance
        except BrokerError as exc:
            error = exc
        with self._db.transaction() as tx:
            order = self._orders.require(tx, coid)
            if error is not None:
                self._attempt(tx, coid, now, ResolutionMethod.BY_CLIENT_ID, AttemptResult.ERROR,
                              error=type(error).__name__)  # fmt: skip
                reason = read_error_halt(error)
                if reason is not None:
                    halt(tx, reason, f"lookup of {coid}: {type(error).__name__}: {error}", now)
                return order.status
            if snap is not None:
                self._attempt(tx, coid, now, ResolutionMethod.BY_CLIENT_ID, AttemptResult.FOUND,
                              broker_order_id=snap.broker_order_id,
                              broker_status=snap.broker_status)  # fmt: skip
                return apply_snapshot(tx, order, snap, portfolio=self._pf, at=now,
                                      source=EventSource.RECOVERY).status  # fmt: skip
            self._attempt(tx, coid, now, ResolutionMethod.BY_CLIENT_ID, AttemptResult.NOT_FOUND,
                          status=order.status.value,
                          send_attempted=order.send_attempted_at is not None)  # fmt: skip
            if order.status is OrderStatus.PENDING_SUBMIT:
                change_status(tx, order, OrderStatus.SUBMIT_AMBIGUOUS, portfolio=self._pf, at=now,
                              source=EventSource.RECOVERY, event="UNCONFIRMED_AFTER_RESTART",
                              send_attempted=order.send_attempted_at is not None)  # fmt: skip
                return OrderStatus.SUBMIT_AMBIGUOUS
            return order.status

    def list_scan(self, coid: str, *, now: datetime) -> AttemptResult:
        assert_no_transaction("order list scan")
        with self._db.read() as s:
            order = self._orders.require(s, coid)
        after = order.created_at - self._config.list_scan_lookback
        try:
            listed = self._read.get_orders(after=after)
        except BrokerError as exc:
            self._record(coid, now, ResolutionMethod.LIST_SCAN, AttemptResult.ERROR,
                         error=type(exc).__name__)  # fmt: skip
            return AttemptResult.ERROR
        match = next((o for o in listed if o.client_order_id == coid), None)
        with self._db.transaction() as tx:
            current = self._orders.require(tx, coid)
            if match is not None:
                self._attempt(tx, coid, now, ResolutionMethod.LIST_SCAN, AttemptResult.FOUND,
                              broker_order_id=match.broker_order_id)  # fmt: skip
                apply_snapshot(tx, current, match, portfolio=self._pf, at=now,
                               source=EventSource.RECOVERY)  # fmt: skip
                return AttemptResult.FOUND
            self._attempt(tx, coid, now, ResolutionMethod.LIST_SCAN, AttemptResult.NOT_FOUND,
                          after=after.isoformat(), orders_scanned=len(listed))  # fmt: skip
        return AttemptResult.NOT_FOUND

    def activity_scan(self, coid: str, *, now: datetime) -> AttemptResult:
        """A complete activity window over the order's lifetime must contain no fill that
        cannot be attributed to a known order (fills name only the broker order id)."""
        assert_no_transaction("activity scan")
        with self._db.read() as s:
            order = self._orders.require(s, coid)
        after = order.created_at - self._config.list_scan_lookback
        until = now - self._config.posting_lag
        if until <= after:
            self._record(coid, now, ResolutionMethod.ACTIVITY_SCAN, AttemptResult.INCOMPLETE,
                         reason="window not yet closed")  # fmt: skip
            return AttemptResult.INCOMPLETE
        try:
            window = collect_window(self._read, after=after, until=until, now=now,
                                    posting_lag=self._config.posting_lag)  # fmt: skip
        except (IncompleteRetrievalError, BrokerError) as exc:
            self._record(coid, now, ResolutionMethod.ACTIVITY_SCAN, AttemptResult.INCOMPLETE,
                         error=type(exc).__name__)  # fmt: skip
            return AttemptResult.INCOMPLETE
        with self._db.transaction() as tx:
            unknown = sorted(
                a.activity_id for a in window.activities
                if a.is_fill and (a.order_id is None
                                  or self._orders.get_by_broker_id(tx, a.order_id) is None)
            )  # fmt: skip
            result = (
                AttemptResult.INCOMPLETE if not window.complete
                else AttemptResult.UNATTRIBUTED_FILLS if unknown
                else AttemptResult.CLEAN
            )  # fmt: skip
            self._attempt(tx, coid, now, ResolutionMethod.ACTIVITY_SCAN, result,
                          after=after.isoformat(), until=until.isoformat(),
                          activities=len(window.activities), unattributed=unknown)  # fmt: skip
        return result

    # ------------------------------------------------------------- evidence
    def evidence(self, s: Session, coid: str, *, now: datetime | None = None) -> AbsenceEvidence:
        order = self._orders.require(s, coid)
        sent = last_possible_send(order)
        ready_at = sent + self._config.absent_window
        attempts = [a for a in self._attempts.list_for_order(s, coid) if a.ts >= sent]
        lookups = [a for a in attempts if a.method is ResolutionMethod.BY_CLIENT_ID]
        not_found = [a for a in lookups if a.result == AttemptResult.NOT_FOUND]
        latest = max((a.ts for a in not_found), default=None)

        list_ok = max(
            (a.ts for a in attempts if a.method is ResolutionMethod.LIST_SCAN
             and a.result == AttemptResult.NOT_FOUND and a.ts >= ready_at),
            default=None,
        )  # fmt: skip
        # A clean activity scan counts only if its closed window reaches past the
        # absence window (otherwise it may not even cover the send).
        activity_ok = max(
            (a.ts for a in attempts if a.method is ResolutionMethod.ACTIVITY_SCAN
             and a.result == AttemptResult.CLEAN
             and datetime.fromisoformat(str((a.detail or {}).get("until"))) >= ready_at),
            default=None,
        )  # fmt: skip
        recon_ok = _reconciliation_supports_absence(s, coid)
        if (
            recon_ok is not None
            and now is not None
            and (now - recon_ok > self._config.reconciliation_max_age or recon_ok > now)
        ):
            recon_ok = None  # stale evidence is never relied on
        missing: list[str] = []
        if order.status is not OrderStatus.SUBMIT_AMBIGUOUS:
            missing.append(f"status is {order.status.value}, not SUBMIT_AMBIGUOUS")
        if any(a.result == AttemptResult.FOUND for a in attempts):
            missing.append("a lookup or scan found the order")
        if len(not_found) < self._config.ambiguous.min_absent_lookups:
            missing.append(f"{len(not_found)} of {self._config.ambiguous.min_absent_lookups} "
                           "not-found lookups")  # fmt: skip
        if latest is None or latest < ready_at:
            missing.append(f"no not-found lookup at or after {ready_at.isoformat()}")
        earliest = min((a.ts for a in not_found), default=None)
        if earliest is None or latest is None or latest - earliest < self._config.absent_window:
            missing.append("not-found lookups do not span the absence window")
        if list_ok is None:
            missing.append("no clean order-list scan after the absence window")
        if activity_ok is None:
            missing.append("no complete, clean activity scan reaching past the absence window")
        evidence_times = [t for t in (latest, list_ok, activity_ok) if t is not None]
        if recon_ok is None or (evidence_times and recon_ok < max(evidence_times)):
            missing.append("no clean, fresh reconciliation run after the evidence was gathered")
        return AbsenceEvidence(coid, sent, len(not_found), latest, list_ok, activity_ok,
                               recon_ok, tuple(missing))  # fmt: skip

    def confirm_absent(self, coid: str, *, operator: str, now: datetime) -> AbsenceEvidence:
        """Operator action (U4). Refuses — and records the refusal — unless every piece of
        evidence is present; on success releases the reservation."""
        if not operator.strip():
            raise ValueError("an operator identity is required")
        with self._db.transaction() as tx:
            ev = self.evidence(tx, coid, now=now)
            if not ev.complete:
                self._attempt(tx, coid, now, ResolutionMethod.OPERATOR, AttemptResult.REFUSED,
                              operator=operator, **ev.to_dict())  # fmt: skip
                return ev
            order = self._orders.require(tx, coid)
            self._attempt(tx, coid, now, ResolutionMethod.OPERATOR,
                          AttemptResult.CONFIRMED_ABSENT, operator=operator,
                          **ev.to_dict())  # fmt: skip
            change_status(tx, order, OrderStatus.CONFIRMED_ABSENT, portfolio=self._pf, at=now,
                          source=EventSource.OPERATOR, event="CONFIRMED_ABSENT",
                          operator=operator, evidence=ev.to_dict())  # fmt: skip
        return ev

    # ----------------------------------------------------- cycle & escalation
    def cycle(self, *, now: datetime, force: bool = False) -> RecoveryReport:
        """Look up every unresolved order (respecting the lookup interval unless forced),
        then escalate any unresolved past the limit. Never submits anything."""
        report = RecoveryReport()
        with self._db.read() as s:
            pending = [o for o in self._orders.list_working(s) if o.status in AMBIGUOUS_STATUSES]
            last = {
                o.client_order_id: max(
                    (a.ts for a in self._attempts.list_for_order(s, o.client_order_id)
                     if a.method is ResolutionMethod.BY_CLIENT_ID), default=None)
                for o in pending
            }  # fmt: skip
        for order in pending:
            previous = last[order.client_order_id]
            if force or previous is None or now - previous >= self._config.lookup_interval:
                report.looked_up[order.client_order_id] = self.lookup(order.client_order_id,
                                                                      now=now)  # fmt: skip
        report.escalated = self.escalate(now=now)
        return report

    def escalate(self, *, now: datetime) -> list[str]:
        escalated: list[str] = []
        with self._db.transaction() as tx:
            for order in self._orders.list_working(tx):
                if order.status not in AMBIGUOUS_STATUSES:
                    continue
                if now - last_possible_send(order) <= self._config.escalate_after:
                    continue
                state = TradingStateRepository().get(tx)
                if (state.halted and state.reason_code == HaltReason.AMBIGUOUS_ORDER_TIMEOUT
                        and order.client_order_id in (state.reason_detail or "")):  # fmt: skip
                    continue
                halt(tx, HaltReason.AMBIGUOUS_ORDER_TIMEOUT,
                     f"order {order.client_order_id} unresolved since "
                     f"{last_possible_send(order).isoformat()}", now)  # fmt: skip
                record_event(tx, order.client_order_id, at=now, source=EventSource.RECOVERY,
                             event="ESCALATED",
                             reason=HaltReason.AMBIGUOUS_ORDER_TIMEOUT.value)  # fmt: skip
                escalated.append(order.client_order_id)
        return escalated

    # --------------------------------------------------------------- helpers
    def _attempt(
        self, tx: Tx, coid: str, now: datetime, method: ResolutionMethod, result: AttemptResult,
        **detail: object,
    ) -> None:  # fmt: skip
        self._attempts.append(tx, ResolutionAttempt(coid, now, method, result.value,
                                                    _jsonable(detail)))  # fmt: skip

    def _record(
        self, coid: str, now: datetime, method: ResolutionMethod, result: AttemptResult,
        **detail: object,
    ) -> None:  # fmt: skip
        with self._db.transaction() as tx:
            self._attempt(tx, coid, now, method, result, **detail)


def _reconciliation_supports_absence(s: Session, coid: str) -> datetime | None:
    """Timestamp of the latest reconciliation run if it supports declaring ``coid``
    absent: INCONCLUSIVE with exactly one non-advisory finding — this order's
    ``AMBIGUOUS_ORDER_UNRESOLVED`` marked ``absence_consistent`` (complete order list
    without it, mature complete activity history, cash/positions/orders/activities all
    reconciling with it assumed absent — §10.3). Anything else never does."""
    run = ReconciliationRunsRepository().latest(s)
    if run is None or run.result is not ReconciliationResult.INCONCLUSIVE:
        # A CLEAN run cannot exist while this order is unresolved, and a MISMATCH never
        # supports anything: only the order-specific INCONCLUSIVE run below qualifies.
        return None
    rows = s.fetch_all(
        "SELECT code, severity, subject, detail_json FROM reconciliation_findings "
        "WHERE run_id = ? AND severity <> 'ADVISORY'",
        (run.id,),
    )
    if len(rows) != 1 or str(rows[0][0]) != "AMBIGUOUS_ORDER_UNRESOLVED":
        return None  # any unrelated INCONCLUSIVE (or another unresolved order) disqualifies
    mine = [r for r in rows if r[2] == coid]
    if len(mine) != 1:
        return None
    detail = json.loads(str(mine[0][3] or "{}"))
    return run.ts if detail.get("absence_consistent") == "True" else None


def recover_after_restart(
    recovery: AmbiguousOrderRecovery, sync: OrderSync, *, now: datetime
) -> tuple[RecoveryReport, SyncReport]:
    """Startup steps 5-6 (§10.1): resolve every unconfirmed order by lookup (forced), then
    synchronize activities and order states, then escalate. Never submits or resends."""
    first = recovery.cycle(now=now, force=True)
    synced = sync.sync(now=now)
    first.escalated = sorted({*first.escalated, *recovery.escalate(now=now)})
    return first, synced


def _jsonable(detail: dict[str, object]) -> dict[str, Any]:
    return {k: (str(v) if not isinstance(v, (str, int, bool, list, dict, type(None))) else v)
            for k, v in detail.items()}  # fmt: skip


__all__ = [
    "AbsenceEvidence",
    "AmbiguousOrderRecovery",
    "AttemptResult",
    "RecoveryReport",
    "last_possible_send",
    "recover_after_restart",
]
