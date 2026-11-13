"""Order and activity synchronization (ARCHITECTURE §6.4-6.5, §8.4, §10.4, §12.4 tx C/D).

One :meth:`OrderSync.sync` pass, with every broker read outside any transaction:

1. **Activities.** Read the window from the coverage marker (minus the overlap) to
   ``now``. Each activity is booked in its own transaction (tx C) through the virtual
   portfolio, which is replay-safe by activity id: an identical replay is a no-op, a
   replay with different content is a ``CONFLICT`` and halts. Fills are applied in
   execution-time order; one that executed before already-booked fills is still
   recorded and halts (out-of-order). Fills are always recorded **before** any risk or
   invariant evaluation.
2. **Attribution.** FILL activities identify the broker order id only. A fill for a
   known order is booked against it; for an unknown id the broker's order list is
   consulted: a bot order still awaiting adoption is adopted first; an unknown bot id
   becomes an ``ORPHAN`` record; a non-bot or unidentifiable order is preserved as an
   unexplained activity. The last two halt (``RECON_EXECUTION``).
3. **Coverage.** Only after every activity of a tail-confirmed window is committed does
   the marker advance, to ``now - posting_lag`` (never past what is closed). A crash in
   between only causes an idempotent re-read.
4. **Orders.** Each working order with a broker id is refreshed by client id and its
   status applied with fill gating (:mod:`swing_trading_bot.oms.lifecycle`): terminal
   statuses — and the reservation release — wait until all reported fills are booked.
5. **Invariants** run after commit; a violation is a persistent ``LEDGER_INVARIANT``
   halt (the confirmed events stay booked).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum

from swing_trading_bot.broker.activity_feed import collect_window
from swing_trading_bot.broker.errors import (
    BrokerError,
    IncompleteRetrievalError,
)
from swing_trading_bot.broker.models import BrokerActivity
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.domain.enums import ActivityAttribution, HaltReason, OrderStatus
from swing_trading_bot.domain.models import BrokerOrderSnapshot
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES
from swing_trading_bot.ledger.activity_coverage import advance_coverage, complete_through
from swing_trading_bot.ledger.baselines import is_baseline_reflected, reflected_activities
from swing_trading_bot.ledger.db import Database, assert_no_transaction
from swing_trading_bot.ledger.portfolio import (
    CashActivity,
    CashActivityKind,
    ConfirmedFill,
    FillOutcome,
    VirtualPortfolio,
)
from swing_trading_bot.ledger.repositories import (
    ActivitiesRepository,
    ActivityRecord,
    IngestResult,
    OrdersRepository,
)
from swing_trading_bot.oms.classify import classify_cash_activity
from swing_trading_bot.oms.lifecycle import (
    ApplyOutcome,
    EventSource,
    apply_snapshot,
    halt,
    read_error_halt,
)
from swing_trading_bot.oms.order_manager import CrashHook, OmsConfig

_REFRESH: tuple[OrderStatus, ...] = (
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_CANCEL,
)
_CASH_KINDS = {"FEE": CashActivityKind.FEE, "DIV": CashActivityKind.DIVIDEND}


class SyncStatus(StrEnum):
    OK = "OK"
    INCOMPLETE = "INCOMPLETE"
    HALTED = "HALTED"


@dataclass(slots=True)
class SyncReport:
    status: SyncStatus = SyncStatus.OK
    fills: dict[str, FillOutcome] = field(default_factory=dict)
    cash_activities: int = 0
    unclassified: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    transitions: dict[str, OrderStatus] = field(default_factory=dict)
    awaiting_fills: list[str] = field(default_factory=list)
    not_visible: list[str] = field(default_factory=list)
    coverage_advanced: bool = False
    problems: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    """Activity ids replayed with different content (any type): always halts."""

    def mark(self, status: SyncStatus, problem: str) -> None:
        self.problems.append(problem)
        if self.status is not SyncStatus.HALTED:
            self.status = status


class OrderSync:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        read: BrokerReadPort,
        config: OmsConfig,
        *,
        crash: CrashHook | None = None,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._read = read
        self._config = config
        self._crash = crash or (lambda _point: None)
        self._orders = OrdersRepository()

    def sync(self, *, now: datetime) -> SyncReport:
        assert_no_transaction("sync")
        report = SyncReport()
        self._activities(report, now)
        if report.status is not SyncStatus.HALTED:
            self._refresh_orders(report, now)
        invariants = self._pf.check_invariants(now=now)
        if not invariants.ok:
            report.mark(SyncStatus.HALTED, "ledger invariants violated")
        return report

    # ------------------------------------------------------------ activities
    def _activities(self, report: SyncReport, now: datetime) -> None:
        with self._db.read() as s:
            marker = complete_through(s)
            created = self._pf.portfolio(s).created_at
        after = (marker - self._config.activity_overlap) if marker is not None else created
        try:
            window = collect_window(self._read, after=after, until=now, now=now,
                                    posting_lag=self._config.posting_lag)  # fmt: skip
            activities, tail_confirmed = window.activities, window.tail_confirmed
        except IncompleteRetrievalError as exc:
            activities = tuple(a for a in exc.partial if isinstance(a, BrokerActivity))
            tail_confirmed = False
            cause = exc.__cause__
            if cause is not None and read_error_halt(cause) is not None:
                # Not a transient gap: bad credentials or a malformed payload halt (§10.5).
                self._read_failure(report, "activities", cause, now)
            else:
                report.mark(SyncStatus.INCOMPLETE, f"activities: {exc}")
        except BrokerError as exc:
            self._read_failure(report, "activities", exc, now)
            return
        if not self._ingest_all(activities, after, report, now):
            return
        closed_until = now - self._config.posting_lag
        if tail_confirmed and closed_until > after:
            with self._db.transaction() as tx:
                report.coverage_advanced = advance_coverage(
                    tx, after=after, until=closed_until, complete=True, at=now
                )

    def ingest(
        self, activities: tuple[BrokerActivity, ...], *, after: datetime, now: datetime
    ) -> SyncReport:
        """Book a batch of already-read activities (e.g. a historical re-scan window).
        Idempotent: activities already stored are DUPLICATE no-ops."""
        assert_no_transaction("activity ingestion")
        report = SyncReport()
        self._ingest_all(activities, after, report, now)
        return report

    def _ingest_all(
        self,
        activities: tuple[BrokerActivity, ...],
        after: datetime,
        report: SyncReport,
        now: datetime,
    ) -> bool:
        snapshots = self._order_lookup(activities, after, report, now)
        if snapshots is None:
            return False
        for activity in sorted(activities, key=_execution_order):
            # Confirmed events are booked even after a halt: nothing is skipped.
            self._ingest(activity, snapshots, report, now)
        return True

    def _order_lookup(
        self,
        activities: tuple[BrokerActivity, ...],
        after: datetime,
        report: SyncReport,
        now: datetime,
    ) -> dict[str, BrokerOrderSnapshot] | None:
        """Broker orders for fills whose broker order id is not yet known locally."""
        with self._db.read() as s:
            unknown = {
                a.order_id
                for a in activities
                if a.is_fill
                and a.order_id is not None
                and self._orders.get_by_broker_id(s, a.order_id) is None
            }
        if not unknown:
            return {}
        try:
            listed = self._read.get_orders(after=after - self._config.list_scan_lookback)
        except BrokerError as exc:
            self._read_failure(report, "orders for fill attribution", exc, now)
            return None
        return {o.broker_order_id: o for o in listed if o.broker_order_id in unknown}

    def _ingest(
        self,
        activity: BrokerActivity,
        snapshots: dict[str, BrokerOrderSnapshot],
        report: SyncReport,
        now: datetime,
    ) -> None:
        with self._db.read() as s:
            reflected = is_baseline_reflected(
                reflected_activities(s), activity.activity_id, activity.raw_json
            )
        if reflected:
            # Observed by init-ledger and already inside the INIT baseline cash (Step
            # 15B.13A): recorded as explained history, never booked, never counted. A
            # replay with different content still conflicts and halts.
            if self._preserve(activity, now, ActivityAttribution.EXTERNAL):
                report.conflicts.append(activity.activity_id)
                report.mark(SyncStatus.HALTED, f"activity {activity.activity_id} conflict")
            return
        if activity.is_fill:
            self._ingest_fill(activity, snapshots, report, now)
            return
        with self._db.read() as s:
            stored = ActivitiesRepository().get(s, activity.activity_id)
            verdict = None if stored is not None else classify_cash_activity(s, activity)
        attribution = stored.attribution if stored is not None else verdict.attribution  # type: ignore[union-attr]
        bookable = activity.activity_type in _CASH_KINDS and activity.net_amount is not None
        if attribution is ActivityAttribution.ATTRIBUTABLE and bookable:
            # First booking, or a replay the portfolio checks for identical content.
            self._book_cash(activity, report, now)
            return
        # Recorded without ledger effect; a replay is compared with the stored row.
        if self._preserve(activity, now, attribution):
            report.conflicts.append(activity.activity_id)
            report.mark(SyncStatus.HALTED, f"activity {activity.activity_id} conflict")
        if stored is None and attribution is ActivityAttribution.UNEXPLAINED:
            report.unclassified.append(activity.activity_id)

    def _book_cash(self, activity: BrokerActivity, report: SyncReport, now: datetime) -> None:
        assert activity.net_amount is not None  # noqa: S101 - checked by the caller
        when = activity.occurred_at or _midday(activity)
        try:
            kind = _CASH_KINDS[activity.activity_type]
            cash = CashActivity(activity.activity_id, kind, activity.net_amount, when,
                                activity.raw_json, activity.symbol)  # fmt: skip
        except ValueError:  # e.g. a positive FEE: not a shape we can book
            self._preserve(activity, now, ActivityAttribution.UNEXPLAINED)
            report.unclassified.append(activity.activity_id)
            return
        result = self._pf.process_cash_activity(cash, now=now)
        report.cash_activities += 1
        if result.outcome is FillOutcome.CONFLICT:
            report.conflicts.append(activity.activity_id)
        if result.halted:
            report.mark(SyncStatus.HALTED, f"cash activity {activity.activity_id}")

    def _ingest_fill(
        self,
        activity: BrokerActivity,
        snapshots: dict[str, BrokerOrderSnapshot],
        report: SyncReport,
        now: datetime,
    ) -> None:
        a = activity
        if (a.order_id is None or a.symbol is None or a.side is None or a.qty is None
                or a.price is None or a.occurred_at is None):  # fmt: skip
            self._preserve(a, now)
            with self._db.transaction() as tx:
                halt(tx, HaltReason.BROKER_PAYLOAD, f"fill {a.activity_id} is incomplete", now)
            report.mark(SyncStatus.HALTED, f"incomplete fill {a.activity_id}")
            return
        coid = self._attribute(a.order_id, snapshots, report, now)
        fill = ConfirmedFill(
            activity_id=a.activity_id,
            client_order_id=coid,
            broker_order_id=a.order_id,
            symbol=a.symbol,
            side=a.side,
            qty=a.qty,
            price=a.price,
            fees=ZERO,
            transaction_time=a.occurred_at,
            raw_json=a.raw_json,
        )
        result = self._pf.process_fill(fill, now=now, hook=lambda p: self._crash(f"fill:{p}"))
        report.fills[a.activity_id] = result.outcome
        if result.halted:
            report.mark(SyncStatus.HALTED, f"fill {a.activity_id}: {result.outcome.value}")

    def _attribute(
        self,
        broker_order_id: str,
        snapshots: dict[str, BrokerOrderSnapshot],
        report: SyncReport,
        now: datetime,
    ) -> str | None:
        with self._db.read() as s:
            known = self._orders.get_by_broker_id(s, broker_order_id)
        if known is not None:
            return known.client_order_id
        snap = snapshots.get(broker_order_id)
        if snap is None:
            return None  # unidentifiable: booked as UNATTRIBUTED (preserved, halts)
        with self._db.transaction() as tx:
            local = self._orders.get(tx, snap.client_order_id)
            if local is not None and local.status in AMBIGUOUS_STATUSES:
                apply_snapshot(tx, local, snap, portfolio=self._pf, at=now,
                               source=EventSource.RECOVERY)  # fmt: skip
                report.adopted.append(local.client_order_id)
        return snap.client_order_id

    def _preserve(
        self,
        a: BrokerActivity,
        now: datetime,
        attribution: ActivityAttribution = ActivityAttribution.UNEXPLAINED,
    ) -> bool:
        """Record an activity without any ledger effect (EXTERNAL: explained, not the
        bot's; UNEXPLAINED: blocks entries and fails reconciliation). Idempotent by id."""
        with self._db.transaction() as tx:
            result = ActivitiesRepository().record(
                tx,
                ActivityRecord(
                    activity_id=a.activity_id,
                    activity_type=a.activity_type,
                    ts=a.occurred_at or _midday(a),
                    attribution=attribution,
                    raw_json=a.raw_json,
                    ingested_at=now,
                    symbol=a.symbol,
                    qty=a.qty,
                    price=a.price,
                    net_amount=a.net_amount,
                    side=a.side,
                    broker_order_id=a.order_id,
                ),
            )
            conflict = result is IngestResult.CONFLICT
            if conflict:
                halt(tx, HaltReason.RECON_EXECUTION,
                     f"activity {a.activity_id} replayed with different content", now)  # fmt: skip
        return conflict

    # ---------------------------------------------------------------- orders
    def _refresh_orders(self, report: SyncReport, now: datetime) -> None:
        with self._db.read() as s:
            working = [
                o for o in self._orders.list_working(s)
                if o.status in _REFRESH and o.broker_order_id is not None
            ]  # fmt: skip
        for order in working:
            try:
                snap = self._read.get_order_by_client_id(order.client_order_id)
            except BrokerError as exc:
                self._read_failure(report, f"order {order.client_order_id}", exc, now)
                return
            if snap is None:
                report.not_visible.append(order.client_order_id)
                continue
            with self._db.transaction() as tx:
                applied = apply_snapshot(tx, order, snap, portfolio=self._pf, at=now,
                                         source=EventSource.BROKER)  # fmt: skip
            if applied.outcome is ApplyOutcome.HALTED:
                report.mark(SyncStatus.HALTED, f"order {order.client_order_id}")
            elif applied.outcome is ApplyOutcome.AWAITING_FILLS:
                report.awaiting_fills.append(order.client_order_id)
            if applied.status is not order.status:
                report.transitions[order.client_order_id] = applied.status

    def _read_failure(
        self, report: SyncReport, what: str, exc: BaseException, now: datetime
    ) -> None:
        reason = read_error_halt(exc)
        if reason is None:
            report.mark(SyncStatus.INCOMPLETE, f"{what}: {type(exc).__name__}")
            return
        with self._db.transaction() as tx:
            halt(tx, reason, f"{what}: {type(exc).__name__}: {exc}", now)
        report.mark(SyncStatus.HALTED, f"{what}: {reason.value}")


def _execution_order(a: BrokerActivity) -> tuple[int, datetime | None]:
    return (0 if a.occurred_at is not None else 1, a.occurred_at)


def _midday(a: BrokerActivity) -> datetime:
    return datetime.combine(a.activity_date, time(12), tzinfo=MARKET_TZ)


__all__ = ["OrderSync", "SyncReport", "SyncStatus"]
