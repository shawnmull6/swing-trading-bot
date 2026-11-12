"""Broker reconciliation (ARCHITECTURE §8.2-8.4, M1 step 13). Read-only at the broker.

One :meth:`Reconciler.run` does, with every broker read outside any transaction:

1. **Ingest.** A forward synchronization (overlap window to now, order refresh), then the
   historical re-scans due under :class:`~swing_trading_bot.broker.activity_feed.RescanPolicy`
   — overlap, periodic lookback and the full history since the active broker baseline.
   Confirmed broker events are booked or preserved (idempotent by activity id) BEFORE
   anything is judged, so unexpected events are never lost because a run halts.
2. **Observe.** Account, account configuration, positions, open orders and the complete
   order list since the baseline. Ambiguous local orders the list proves to exist are
   adopted. Settlement dates that can now be resolved from the calendar are resolved
   (never inferred from weekdays).
3. **Settle the snapshot.** The activity head is read again; if new activities appeared
   they are ingested and the observation repeats (``max_snapshot_retries``), otherwise
   the run is INCONCLUSIVE (activity churn).
4. **Judge** (pure, on one ledger read snapshot) — every finding has a category, a code,
   a subject and a severity:

   * MISMATCH — a confirmed, unexplained difference. Persistent halt.
   * INCONCLUSIVE — cannot decide now: a read failed or was incomplete, a fill is
     reported but its activity is not booked, fees are pending, an order outcome is
     unresolved, a settlement date is unknown, the activity head kept moving.
   * ADVISORY — reported only (U6 account protections).

   Outcome: any MISMATCH → MISMATCH (a failed read never hides an independently
   confirmed difference); else any INCONCLUSIVE → INCONCLUSIVE; else CLEAN (stored as
   ``OK``). A check whose inputs were not completely retrieved is not evaluated — it is
   reported INCONCLUSIVE instead of guessed.
5. **Persist** in one transaction: the ``reconciliation_runs`` row (result + evidence
   boundaries in ``diff_json``), every finding (``reconciliation_findings``), and — for
   MISMATCH — a persistent halt referencing the run; for the ``max_consecutive_inconclusive``-th
   INCONCLUSIVE in a row, a ``RECON_INCONCLUSIVE_REPEAT`` halt.

Never: submits, cancels, edits a baseline, rewrites a fill or cash entry, uses broker
buying power, or treats a cursor, a 404 or an incomplete scan as proof of anything.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final

from swing_trading_bot.broker.activity_feed import RescanPolicy, collect_window, rescans_due
from swing_trading_bot.broker.errors import BrokerError, IncompleteRetrievalError
from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot, BrokerActivity
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.domain.enums import (
    ActivityAttribution,
    HaltReason,
    OrderStatus,
    ReconciliationResult,
    Side,
)
from swing_trading_bot.domain.ids import is_bot_client_order_id
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, PRICE_QUANTUM, ZERO, is_whole
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES, TERMINAL_STATUSES
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.acknowledgements import ACKNOWLEDGED_ACTIVITY_SQL, acknowledged_order
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.baselines import (
    BrokerBaseline,
    BrokerBaselinesRepository,
    cash_residual,
    movements_since,
)
from swing_trading_bot.ledger.codec import ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Database, Session, Tx, assert_no_transaction
from swing_trading_bot.ledger.portfolio import VirtualPortfolio, market_date
from swing_trading_bot.ledger.repositories import (
    OrderRecord,
    OrdersRepository,
    PositionsRepository,
    ReconciliationRunsRepository,
    SyncStateRepository,
    TradingStateRepository,
    canonical_json,
)
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID
from swing_trading_bot.oms.lifecycle import EventSource, apply_snapshot
from swing_trading_bot.oms.order_manager import CrashHook, OmsConfig
from swing_trading_bot.oms.sync import OrderSync, SyncReport, SyncStatus
from swing_trading_bot.risk.gates import EXPECTED_ENVIRONMENT

LAST_PERIODIC_RESCAN: Final = "activities.last_periodic_rescan"
LAST_RECONCILIATION_RUN: Final = "reconciliation.last_run_id"
RECON_ACTOR: Final = "reconciler"


class Severity(StrEnum):
    MISMATCH = "MISMATCH"
    INCONCLUSIVE = "INCONCLUSIVE"
    ADVISORY = "ADVISORY"


class Category(StrEnum):
    ACCOUNT = "ACCOUNT"
    RETRIEVAL = "RETRIEVAL"
    ORDER = "ORDER"
    EXECUTION = "EXECUTION"
    ACTIVITY = "ACTIVITY"
    POSITION = "POSITION"
    CASH = "CASH"
    SETTLEMENT = "SETTLEMENT"
    LEDGER = "LEDGER"


class Outcome(StrEnum):
    CLEAN = "CLEAN"
    INCONCLUSIVE = "INCONCLUSIVE"
    MISMATCH = "MISMATCH"

    @property
    def stored(self) -> ReconciliationResult:
        return {
            Outcome.CLEAN: ReconciliationResult.OK,
            Outcome.INCONCLUSIVE: ReconciliationResult.INCONCLUSIVE,
            Outcome.MISMATCH: ReconciliationResult.MISMATCH,
        }[self]


_HALT_FOR: Final[dict[Category, HaltReason]] = {
    Category.ACCOUNT: HaltReason.RECON_ACCOUNT,
    Category.LEDGER: HaltReason.LEDGER_INVARIANT,
    Category.EXECUTION: HaltReason.RECON_EXECUTION,
    Category.ACTIVITY: HaltReason.RECON_EXECUTION,
    Category.ORDER: HaltReason.RECON_ORDER,
    Category.POSITION: HaltReason.RECON_POSITION,
    Category.CASH: HaltReason.RECON_CASH,
    Category.SETTLEMENT: HaltReason.RECON_CASH,
    Category.RETRIEVAL: HaltReason.RECON_EXECUTION,
}
_HALT_PRIORITY: Final = list(_HALT_FOR)


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    category: Category
    code: str
    severity: Severity
    subject: str = ""
    detail: tuple[tuple[str, str], ...] = ()

    def detail_dict(self) -> dict[str, str]:
        return dict(self.detail)


def finding(
    category: Category, code: str, severity: Severity, subject: str = "", **detail: object
) -> Finding:
    return Finding(
        category, code, severity, subject, tuple(sorted((k, str(v)) for k, v in detail.items()))
    )


@dataclass(frozen=True, slots=True)
class ReconConfig:
    oms: OmsConfig
    rescan: RescanPolicy
    max_snapshot_retries: int = 3
    max_consecutive_inconclusive: int = 5
    clean_max_age: timedelta = timedelta(minutes=10)
    inconclusive_cycle_interval: timedelta = timedelta(seconds=60)
    """INCONCLUSIVE runs closer together than this count as one reconciliation cycle."""
    """How long a CLEAN run may be relied on by a dependent operation (resume, absence)."""


@dataclass(slots=True)
class _Window:
    kind: str
    after: datetime | None
    until: datetime
    complete: bool
    activities: tuple[BrokerActivity, ...] = ()
    error: str | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "after": None if self.after is None else self.after.isoformat(),
            "until": self.until.isoformat(),
            "complete": self.complete,
            "activities": len(self.activities),
            "error": self.error,
        }


@dataclass(slots=True)
class Observation:
    """Everything read from the broker for one run, with its evidence boundaries."""

    at: datetime
    attempts: int = 0
    account: AccountSnapshot | None = None
    account_config: AccountConfigSnapshot | None = None
    positions: list[BrokerPosition] | None = None
    open_orders: list[BrokerOrderSnapshot] | None = None
    orders: list[BrokerOrderSnapshot] | None = None
    orders_after: datetime | None = None
    baseline_at: datetime | None = None
    inception_at: datetime | None = None
    windows: list[_Window] = field(default_factory=list)
    head_stable: bool = False
    ingest: list[SyncReport] = field(default_factory=list)
    read_failures: list[Finding] = field(default_factory=list)

    @property
    def history(self) -> _Window | None:
        return next((w for w in self.windows if w.kind == "FULL_HISTORY"), None)

    def broker_hash(self) -> str:
        """Hash of the broker *state* (account, positions, orders). Activities are not
        included: they are all ingested, so the ledger fingerprint binds them, and the
        closed history window legitimately grows as time passes."""
        payload = {
            "account": None
            if self.account is None
            else [self.account.account_id, str(self.account.cash)],
            "positions": None
            if self.positions is None
            else sorted((p.symbol, str(p.qty), str(p.avg_entry_price)) for p in self.positions),
            "orders": None
            if self.orders is None
            else sorted(
                (o.client_order_id, o.broker_status, str(o.filled_qty)) for o in self.orders
            ),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    run_id: int
    outcome: Outcome
    findings: tuple[Finding, ...]
    evidence: dict[str, Any]
    halted: HaltReason | None

    def codes(self, severity: Severity | None = None) -> set[str]:
        return {f.code for f in self.findings if severity is None or f.severity is severity}


class Reconciler:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        read: BrokerReadPort,
        config: ReconConfig,
        *,
        crash: CrashHook | None = None,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._read = read
        self._config = config
        self._crash = crash or (lambda _point: None)
        self._sync = OrderSync(db, portfolio, read, config.oms, crash=crash)
        self._orders = OrdersRepository()

    # ================================================================== run
    def run(self, *, now: datetime) -> ReconciliationReport:
        assert_no_transaction("reconciliation")
        with self._db.read() as s:
            pid = self._pf.portfolio(s).id or 0
            baseline = BrokerBaselinesRepository().active(s, pid)
        obs = self._observe(baseline, now)
        self._crash("recon:after_observation")
        with self._db.read() as s:
            findings = sorted(set(self._judge(s, baseline, obs, now)))
        outcome = _outcome(findings)
        evidence = self._evidence(baseline, obs, findings)
        self._crash("recon:before_persist")
        return self._persist(outcome, findings, evidence, now)

    # ============================================================ observe
    def _observe(self, baseline: BrokerBaseline | None, now: datetime) -> Observation:
        obs = Observation(at=now)
        for attempt in range(1, self._config.max_snapshot_retries + 1):
            obs.attempts = attempt
            obs.read_failures = []
            obs.ingest = [self._sync.sync(now=now)]
            obs.windows = self._rescans(baseline, now, obs)
            self._read_broker(baseline, obs)
            self._adopt_from_list(obs, now)
            self._pf.resolve_settlements(now=now)
            if self._head_stable(now):
                obs.head_stable = True
                break
        return obs

    def _rescans(
        self, baseline: BrokerBaseline | None, now: datetime, obs: Observation
    ) -> list[_Window]:
        with self._db.read() as s:
            marker = complete_through(s)
            last_periodic = SyncStateRepository().get(s, LAST_PERIODIC_RESCAN)
            inception = BrokerBaselinesRepository().inception(s, self._pf.portfolio(s).id or 0)
        obs.inception_at = inception
        planned = rescans_due(
            self._config.rescan,
            now=now,
            complete_through=marker,
            last_periodic_rescan=None if last_periodic is None else ts_from_db(last_periodic),
            baseline_at=None if baseline is None else baseline.taken_at,
            reconciling=True,
        )
        windows: list[_Window] = []
        for plan in planned:
            after = plan.after
            if plan.kind == "FULL_HISTORY" and inception is not None:
                # Always from the FIRST baseline: an approved rebaseline never removes
                # an earlier time range from evaluation (§12.19).
                after = inception
            if after is not None and after >= plan.until:
                # Nothing closed yet in this range: an empty, complete closed window.
                windows.append(_Window(plan.kind, after, plan.until, complete=True))
                continue
            try:
                w = collect_window(
                    self._read,
                    after=after,
                    until=plan.until,
                    now=now,
                    posting_lag=self._config.rescan.posting_lag,
                )
            except IncompleteRetrievalError as exc:
                partial = tuple(a for a in exc.partial if isinstance(a, BrokerActivity))
                obs.ingest.append(self._sync.ingest(partial, after=after or now, now=now))
                windows.append(
                    _Window(
                        plan.kind,
                        after,
                        plan.until,
                        complete=False,
                        activities=partial,
                        error=str(exc),
                    )
                )
                continue
            except BrokerError as exc:
                windows.append(
                    _Window(
                        plan.kind,
                        after,
                        plan.until,
                        complete=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            obs.ingest.append(self._sync.ingest(w.activities, after=after or now, now=now))
            windows.append(_Window(plan.kind, after, plan.until, w.complete, w.activities))
            if plan.kind == "PERIODIC" and w.complete:
                with self._db.transaction() as tx:
                    SyncStateRepository().set(tx, LAST_PERIODIC_RESCAN, ts_to_db(now), now)
        return windows

    def _read_broker(self, baseline: BrokerBaseline | None, obs: Observation) -> None:
        obs.account = self._call(obs, "account", self._read.get_account)
        obs.account_config = self._call(obs, "account configuration", self._read.get_account_config)
        obs.positions = self._call(obs, "positions", self._read.get_positions)
        obs.open_orders = self._call(obs, "open orders", self._read.get_open_orders)
        if baseline is None:
            obs.orders = None
            return
        obs.baseline_at = baseline.taken_at
        obs.orders_after = baseline.taken_at - self._config.oms.list_scan_lookback
        after = obs.orders_after
        obs.orders = self._call(obs, "orders", lambda: self._read.get_orders(after=after))

    def _call[T](self, obs: Observation, what: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except IncompleteRetrievalError as exc:
            obs.read_failures.append(
                finding(
                    Category.RETRIEVAL,
                    "INCOMPLETE_RETRIEVAL",
                    Severity.INCONCLUSIVE,
                    what,
                    error=exc,
                )
            )
        except BrokerError as exc:
            obs.read_failures.append(
                finding(
                    Category.RETRIEVAL,
                    "READ_FAILED",
                    Severity.INCONCLUSIVE,
                    what,
                    error=type(exc).__name__,
                )
            )
        return None

    def _adopt_from_list(self, obs: Observation, now: datetime) -> None:
        """Ambiguous or declared-absent local orders that the broker's order list shows
        are adopted (a confirmed broker fact); late appearances halt via the lifecycle."""
        if obs.orders is None:
            return
        listed = {o.client_order_id: o for o in obs.orders}
        with self._db.read() as s:
            candidates = [
                o
                for o in self._orders.list_all(s)
                if (o.status in AMBIGUOUS_STATUSES or o.status is OrderStatus.CONFIRMED_ABSENT)
                and o.client_order_id in listed
            ]
        for order in candidates:
            with self._db.transaction() as tx:
                apply_snapshot(
                    tx,
                    order,
                    listed[order.client_order_id],
                    portfolio=self._pf,
                    at=now,
                    source=EventSource.RECOVERY,
                )

    def _head_stable(self, now: datetime) -> bool:
        """Re-read the activity head; True when nothing new has been posted."""
        after = now - self._config.oms.activity_overlap
        try:
            head = collect_window(
                self._read,
                after=after,
                until=now,
                now=now,
                posting_lag=self._config.rescan.posting_lag,
            )
        except BrokerError:
            return False
        with self._db.read() as s:
            new = [
                a
                for a in head.activities
                if s.fetch_one(
                    "SELECT 1 FROM broker_activities WHERE activity_id = ?", (a.activity_id,)
                )
                is None
            ]
        return head.tail_confirmed and not new

    # =============================================================== judge
    def _judge(
        self, s: Session, baseline: BrokerBaseline | None, obs: Observation, now: datetime
    ) -> list[Finding]:
        out: list[Finding] = list(obs.read_failures)
        if baseline is None:
            out.append(
                finding(
                    Category.ACCOUNT,
                    "BASELINE_MISSING",
                    Severity.INCONCLUSIVE,
                    detail="no active broker baseline: cash cannot be explained",
                )
            )
        if not obs.head_stable:
            out.append(
                finding(
                    Category.RETRIEVAL,
                    "ACTIVITY_HEAD_UNSTABLE",
                    Severity.INCONCLUSIVE,
                    attempts=obs.attempts,
                )
            )
        for report in obs.ingest:
            out.extend(_ingest_findings(report))
        for w in obs.windows:
            if not w.complete:
                out.append(
                    finding(
                        Category.RETRIEVAL,
                        "ACTIVITY_WINDOW_INCOMPLETE",
                        Severity.INCONCLUSIVE,
                        w.kind,
                        error=w.error or "not closed",
                    )
                )
        out.extend(self._account(s, obs))
        lagging = self._fill_lag(s, obs)
        out.extend(lagging.values())
        out.extend(self._orders_check(s, obs, set(lagging)))
        out.extend(self._executions(s, baseline, obs))
        out.extend(self._positions(s, obs, {lagging_symbol(f) for f in lagging.values()}))
        out.extend(self._cash(s, baseline, obs, blocked=bool(lagging)))
        out.extend(self._settlement_and_ledger(s, now))
        return self._ambiguous(s, obs, out)

    def _account(self, s: Session, obs: Observation) -> list[Finding]:
        out: list[Finding] = []
        a = obs.account
        recorded = SyncStateRepository().get(s, BROKER_ACCOUNT_ID)
        if a is not None:
            if recorded is None:
                out.append(finding(Category.ACCOUNT, "IDENTITY_UNRECORDED", Severity.INCONCLUSIVE))
            elif a.account_id != recorded:
                out.append(
                    finding(
                        Category.ACCOUNT,
                        "ACCOUNT_ID_MISMATCH",
                        Severity.MISMATCH,
                        a.account_id,
                        recorded=recorded,
                    )
                )
            expected = EXPECTED_ENVIRONMENT.get(self._config.oms.mode)
            if a.environment != expected:
                out.append(
                    finding(
                        Category.ACCOUNT,
                        "ENVIRONMENT_MISMATCH",
                        Severity.MISMATCH,
                        a.environment,
                        expected=expected,
                    )
                )
            if a.currency != "USD":
                out.append(
                    finding(Category.ACCOUNT, "CURRENCY_UNSUPPORTED", Severity.MISMATCH, a.currency)
                )
            if a.status != "ACTIVE" or a.trading_blocked or a.account_blocked:
                out.append(
                    finding(Category.ACCOUNT, "ACCOUNT_RESTRICTED", Severity.INCONCLUSIVE, a.status)
                )
            if a.accrued_fees != ZERO or a.pending_reg_taf_fees != ZERO:
                out.append(
                    finding(
                        Category.CASH,
                        "FEES_PENDING",
                        Severity.INCONCLUSIVE,
                        accrued=a.accrued_fees,
                        pending=a.pending_reg_taf_fees,
                    )
                )
        c = obs.account_config
        if c is not None and not c.m2_protections_in_place:
            out.append(
                finding(
                    Category.ACCOUNT,
                    "ACCOUNT_PROTECTIONS_NOT_SET",
                    Severity.ADVISORY,
                    no_shorting=c.no_shorting,
                    max_margin_multiplier=c.max_margin_multiplier,
                )
            )
        return out

    def _fill_lag(self, s: Session, obs: Observation) -> dict[str, Finding]:
        """Orders whose broker-reported filled_qty exceeds the booked fills."""
        reported: dict[str, Decimal] = {}
        for o in self._orders.list_all(s):
            if o.broker_filled_qty is not None:
                reported[o.client_order_id] = o.broker_filled_qty
        for snap in obs.orders or []:
            reported[snap.client_order_id] = max(
                reported.get(snap.client_order_id, ZERO), snap.filled_qty
            )
        out: dict[str, Finding] = {}
        for coid, broker_qty in reported.items():
            local = self._orders.get(s, coid)
            booked = ZERO if local is None else local.filled_qty
            if local is not None and broker_qty > booked:
                out[coid] = finding(
                    Category.EXECUTION,
                    "FILL_ACTIVITY_LAG",
                    Severity.INCONCLUSIVE,
                    coid,
                    symbol=local.symbol,
                    broker=broker_qty,
                    booked=booked,
                )
        return out

    def _orders_check(  # noqa: PLR0912 - one branch per documented order finding
        self, s: Session, obs: Observation, lagging: set[str]
    ) -> list[Finding]:
        if obs.orders is None:
            return []  # already INCONCLUSIVE via the read failure; nothing is guessed
        out: list[Finding] = []
        counts = Counter(o.client_order_id for o in obs.orders)
        for coid, n in counts.items():
            if n > 1:
                out.append(
                    finding(
                        Category.ORDER, "DUPLICATE_BROKER_ORDER", Severity.MISMATCH, coid, count=n
                    )
                )
        listed = {o.client_order_id: o for o in obs.orders}
        open_ids = {o.client_order_id for o in obs.open_orders or []}
        open_by_symbol = Counter(o.symbol for o in obs.open_orders or [])
        for symbol, n in open_by_symbol.items():
            if n > 1:
                out.append(
                    finding(
                        Category.ORDER,
                        "MULTIPLE_WORKING_ORDERS",
                        Severity.MISMATCH,
                        symbol,
                        count=n,
                    )
                )
        local = {o.client_order_id: o for o in self._orders.list_all(s)}
        for coid, snap in listed.items():
            if not is_bot_client_order_id(coid):
                payload = external_order_payload(snap)
                if coid not in open_ids and acknowledged_order(s, coid, payload):
                    continue  # this exact, closed order was acknowledged by a rebaseline
                out.append(
                    finding(
                        Category.ORDER,
                        "EXTERNAL_ORDER",
                        Severity.MISMATCH,
                        coid,
                        symbol=snap.symbol,
                        side=snap.side,
                        qty=snap.qty,
                        status=snap.broker_status,
                        payload=payload,
                    )
                )
                continue
            mine = local.get(coid)
            if mine is None:
                out.append(
                    finding(
                        Category.ORDER,
                        "UNEXPECTED_ORDER",
                        Severity.MISMATCH,
                        coid,
                        symbol=snap.symbol,
                        status=snap.broker_status,
                    )
                )
                continue
            out.extend(
                _order_conflicts(mine, snap, is_open=coid in open_ids, lagging=coid in lagging)
            )
        for coid, mine in local.items():
            if coid in listed:
                continue
            if mine.status in (
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_CANCEL,
            ):
                out.append(
                    finding(
                        Category.ORDER,
                        "MISSING_BROKER_ORDER",
                        Severity.MISMATCH,
                        coid,
                        status=mine.status,
                        broker_order_id=mine.broker_order_id,
                    )
                )
            elif (
                mine.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED)
                and obs.orders_after is not None
                and mine.created_at > obs.orders_after
            ):
                out.append(
                    finding(
                        Category.ORDER,
                        "MISSING_BROKER_ORDER",
                        Severity.MISMATCH,
                        coid,
                        status=mine.status,
                    )
                )
            elif mine.status is OrderStatus.ORPHAN:
                out.append(finding(Category.ORDER, "ORPHAN_ORDER", Severity.MISMATCH, coid))
        return out

    def _executions(
        self, s: Session, baseline: BrokerBaseline | None, obs: Observation
    ) -> list[Finding]:
        out: list[Finding] = []
        # Every stored activity, whatever its timestamp: only events acknowledged one by
        # one (with an unchanged payload) are excluded — never a time range.
        columns = "activity_id, activity_type, attribution, symbol, side, qty, net_amount"
        sql = f"SELECT {columns}, {ACKNOWLEDGED_ACTIVITY_SQL} FROM broker_activities ORDER BY ts"  # noqa: S608
        rows = s.fetch_all(sql)
        for aid, kind, attribution, symbol, side, qty, amount, acknowledged in rows:
            if attribution == ActivityAttribution.UNEXPLAINED.value and acknowledged:
                continue
            if attribution == ActivityAttribution.UNEXPLAINED.value:
                category = Category.EXECUTION if kind == "FILL" else Category.CASH
                code = (
                    "UNEXPLAINED_FILL" if kind == "FILL"
                    else "DIVIDEND_ENTITLEMENT_UNVERIFIED" if kind == "DIV"
                    else "UNEXPLAINED_CASH_ACTIVITY"
                )  # fmt: skip
                out.append(
                    finding(
                        category,
                        code,
                        Severity.MISMATCH,
                        str(aid),
                        type=kind,
                        symbol=symbol,
                        side=side,
                        qty=qty,
                        amount=amount,
                    )
                )
            elif attribution == ActivityAttribution.ATTRIBUTABLE.value and kind != "FILL":
                booked = s.fetch_one(
                    "SELECT amount FROM cash_entries WHERE activity_id = ?", (aid,)
                )
                if booked is None or Decimal(str(booked[0])) != Decimal(str(amount)):
                    out.append(
                        finding(
                            Category.CASH,
                            "ATTRIBUTABLE_NOT_BOOKED",
                            Severity.MISMATCH,
                            str(aid),
                            amount=amount,
                        )
                    )
        history = obs.history
        if history is not None and history.complete:
            seen = {a.activity_id: a for a in history.activities}
            fills = s.fetch_all(
                "SELECT activity_id, symbol, side, qty, price, filled_at FROM fills"
            )
            for aid, symbol, side, qty, price, filled_at in fills:
                if obs.inception_at is not None and ts_from_db(filled_at) <= obs.inception_at:
                    continue
                if ts_from_db(filled_at) > history.until:
                    continue  # after the closed history window: checked by later runs
                a = seen.get(str(aid))
                if a is None:
                    out.append(
                        finding(
                            Category.EXECUTION,
                            "LEDGER_FILL_NOT_AT_BROKER",
                            Severity.MISMATCH,
                            str(aid),
                            symbol=symbol,
                        )
                    )
                elif (a.symbol, a.side, a.qty, a.price) != (
                    symbol,
                    Side(str(side)),
                    Decimal(str(qty)),
                    Decimal(str(price)),
                ):
                    out.append(
                        finding(
                            Category.EXECUTION, "FILL_CONTENT_CONFLICT", Severity.MISMATCH, str(aid)
                        )
                    )
        out.extend(_booking_order(s))
        return out

    def _positions(self, s: Session, obs: Observation, lag_symbols: set[str]) -> list[Finding]:
        if obs.positions is None:
            return []
        out: list[Finding] = []
        pid = self._pf.portfolio(s).id or 0
        ledger = {
            p.symbol: p for p in PositionsRepository().list_for_portfolio(s, pid) if p.qty != ZERO
        }
        broker = {p.symbol: p for p in obs.positions if p.qty != ZERO}
        for symbol in sorted(set(ledger) | set(broker)):
            b, mine = broker.get(symbol), ledger.get(symbol)
            bq = ZERO if b is None else b.qty
            lq = ZERO if mine is None else mine.qty
            severity = Severity.INCONCLUSIVE if symbol in lag_symbols else Severity.MISMATCH
            if b is not None and (bq < ZERO or not is_whole(bq)):
                out.append(
                    finding(
                        Category.POSITION, "UNSUPPORTED_POSITION", Severity.MISMATCH, symbol, qty=bq
                    )
                )
                continue
            if bq != lq:
                out.append(
                    finding(
                        Category.POSITION,
                        "POSITION_QTY_MISMATCH",
                        severity,
                        symbol,
                        broker=bq,
                        ledger=lq,
                    )
                )
                continue
            if b is not None and mine is not None and lq > ZERO:
                ledger_avg = DECIMAL_CONTEXT.divide(mine.cost_basis, lq).quantize(
                    PRICE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
                )
                fee_rows = s.fetch_all("SELECT fees FROM fills WHERE symbol = ?", (symbol,))
                has_fees = any(Decimal(str(r[0])) != ZERO for r in fee_rows)
                if ledger_avg != b.avg_entry_price and not has_fees:
                    out.append(
                        finding(
                            Category.POSITION,
                            "POSITION_COST_MISMATCH",
                            severity,
                            symbol,
                            broker_avg=b.avg_entry_price,
                            ledger_avg=ledger_avg,
                        )
                    )
        return out

    def _cash(
        self, s: Session, baseline: BrokerBaseline | None, obs: Observation, *, blocked: bool
    ) -> list[Finding]:
        a = obs.account
        if a is None or baseline is None:
            return []
        history = obs.history
        if history is None or not history.complete:
            return [
                finding(
                    Category.CASH,
                    "CASH_NOT_EVALUATED",
                    Severity.INCONCLUSIVE,
                    reason="activity history incomplete",
                )
            ]
        movements = movements_since(s, baseline)
        residual = cash_residual(s, baseline, a.cash)
        if residual == ZERO:
            return []
        if blocked or a.accrued_fees != ZERO or a.pending_reg_taf_fees != ZERO:
            return [
                finding(
                    Category.CASH,
                    "CASH_RESIDUAL_PENDING",
                    Severity.INCONCLUSIVE,
                    residual=residual,
                    reason="unbooked fills or pending fees",
                )
            ]
        return [
            finding(
                Category.CASH,
                "CASH_UNEXPLAINED",
                Severity.MISMATCH,
                residual=residual,
                broker_cash=a.cash,
                baseline_cash=baseline.cash,
                movements=movements,
            )
        ]

    def _settlement_and_ledger(self, s: Session, now: datetime) -> list[Finding]:
        out: list[Finding] = []
        rows = s.fetch_all(
            "SELECT id, trade_date, kind, amount FROM cash_entries "
            "WHERE settles_on IS NULL ORDER BY id"
        )
        for entry_id, trade_date, kind, amount in rows:
            out.append(
                finding(
                    Category.SETTLEMENT,
                    "SETTLEMENT_DATE_UNKNOWN",
                    Severity.INCONCLUSIVE,
                    str(entry_id),
                    trade_date=trade_date,
                    kind=kind,
                    amount=amount,
                )
            )
        today = market_date(now)
        report = accounting_checker(lambda: today).run(s)
        for v in report.violations:
            out.append(
                finding(
                    Category.LEDGER,
                    "LEDGER_INVARIANT",
                    Severity.MISMATCH,
                    f"{v.invariant}:{v.subject}",
                    detail=v.detail,
                )
            )
        return out

    def _ambiguous(self, s: Session, obs: Observation, out: list[Finding]) -> list[Finding]:
        """Unresolved submissions keep a run INCONCLUSIVE. Each is marked
        ``absence_consistent`` only when everything ELSE is clean and complete: the order
        list (complete) does not contain it, and cash/positions/activities reconcile
        with the order assumed absent (§10.3) — the evidence the operator path needs."""
        pending = [o for o in self._orders.list_all(s) if o.status in AMBIGUOUS_STATUSES]
        if not pending:
            return out
        others_clean = not any(f.severity is not Severity.ADVISORY for f in out)
        listed = {o.client_order_id for o in obs.orders or []}
        history = obs.history
        for order in pending:
            sent = order.send_attempted_at or order.created_at
            mature = (
                history is not None
                and history.complete
                and history.until >= sent + self._config.oms.absent_window
            )
            reasons = [
                why
                for ok, why in (
                    (others_clean, "other findings"),
                    (obs.orders is not None, "order list incomplete"),
                    (order.client_order_id not in listed, "listed at broker"),
                    (mature, "activity coverage not mature"),
                    (len(pending) == 1, "other unresolved submissions"),
                )
                if not ok
            ]
            out.append(
                finding(
                    Category.ORDER,
                    "AMBIGUOUS_ORDER_UNRESOLVED",
                    Severity.INCONCLUSIVE,
                    order.client_order_id,
                    status=order.status,
                    absence_consistent=not reasons,
                    not_consistent_because=",".join(reasons) or "-",
                )
            )
        return out

    # ============================================================== persist
    def _evidence(
        self, baseline: BrokerBaseline | None, obs: Observation, findings: list[Finding]
    ) -> dict[str, Any]:
        a = obs.account
        return {
            "observed_at": obs.at.isoformat(),
            "attempts": obs.attempts,
            "head_stable": obs.head_stable,
            "baseline": None
            if baseline is None
            else {
                "id": baseline.id,
                "taken_at": baseline.taken_at.isoformat(),
                "cash": str(baseline.cash),
            },
            "account": None
            if a is None
            else {
                "id": a.account_id,
                "as_of": a.as_of.isoformat(),
                "cash": str(a.cash),
                "environment": a.environment,
            },
            "orders_scan": {
                "after": None if obs.orders_after is None else obs.orders_after.isoformat(),
                "count": None if obs.orders is None else len(obs.orders),
            },
            "positions": None if obs.positions is None else len(obs.positions),
            "positions_cost": None
            if obs.positions is None
            else str(sum((p.qty * p.avg_entry_price for p in obs.positions), ZERO)),
            "open_orders": None if obs.open_orders is None else len(obs.open_orders),
            "windows": [w.evidence() for w in obs.windows],
            "broker_snapshot_hash": obs.broker_hash(),
            "summary": dict(Counter(f.severity.value for f in findings)),
        }

    def _persist(
        self, outcome: Outcome, findings: list[Finding], evidence: dict[str, Any], now: datetime
    ) -> ReconciliationReport:
        halted: HaltReason | None = None
        with self._db.transaction() as tx:
            cycles = self._inconclusive_cycles(tx, now) if outcome is Outcome.INCONCLUSIVE else 0
            evidence = {**evidence, "inconclusive_cycle": cycles}
            run_id = ReconciliationRunsRepository().append(tx, outcome.stored, evidence, now)
            for f in findings:
                tx.insert(
                    "INSERT INTO reconciliation_findings (run_id, category, code, severity, "
                    "subject, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        f.category.value,
                        f.code,
                        f.severity.value,
                        f.subject or None,
                        canonical_json(f.detail_dict()) if f.detail else None,
                    ),
                )
            SyncStateRepository().set(tx, LAST_RECONCILIATION_RUN, str(run_id), now)
            if outcome is Outcome.MISMATCH:
                halted = self._halt_mismatch(tx, run_id, findings, now)
            elif outcome is Outcome.INCONCLUSIVE:
                halted = self._escalate_inconclusive(tx, run_id, now, cycles)
        return ReconciliationReport(run_id, outcome, tuple(findings), evidence, halted)

    def _halt_mismatch(
        self, tx: Tx, run_id: int, findings: list[Finding], now: datetime
    ) -> HaltReason:
        mismatches = [f for f in findings if f.severity is Severity.MISMATCH]
        first = min(mismatches, key=lambda f: _HALT_PRIORITY.index(f.category))
        reason = _HALT_FOR[first.category]
        detail = (
            f"reconciliation run {run_id}: "
            + "; ".join(sorted({f"{f.code}[{f.subject}]" for f in mismatches}))[:1900]
        )
        TradingStateRepository().halt(
            tx, reason, detail=detail, actor=RECON_ACTOR, at=now, reconciliation_run_id=run_id
        )
        return reason

    def _inconclusive_cycles(self, tx: Tx, now: datetime) -> int:
        """Distinct reconciliation cycles in the current unbroken INCONCLUSIVE streak,
        counting this run. Runs closer than ``inconclusive_cycle_interval`` to the last
        counted one belong to the same cycle (an immediate retry is not new evidence that
        the condition persists); any non-INCONCLUSIVE run ends the streak."""
        cycles, last = 1, now
        for result, ts in tx.fetch_all(
            "SELECT result, ts FROM reconciliation_runs ORDER BY id DESC"
        ):
            if result != ReconciliationResult.INCONCLUSIVE.value:
                break
            at = ts_from_db(ts)
            if last - at >= self._config.inconclusive_cycle_interval:
                cycles, last = cycles + 1, at
        return cycles

    def _escalate_inconclusive(
        self, tx: Tx, run_id: int, now: datetime, cycles: int
    ) -> HaltReason | None:
        limit = self._config.max_consecutive_inconclusive
        if cycles < limit:
            return None
        state = TradingStateRepository().get(tx)
        if state.halted and state.reason_code == HaltReason.RECON_INCONCLUSIVE_REPEAT:
            return None
        TradingStateRepository().halt(
            tx,
            HaltReason.RECON_INCONCLUSIVE_REPEAT,
            actor=RECON_ACTOR,
            at=now,
            detail=f"INCONCLUSIVE for {cycles} consecutive reconciliation cycles "
            f"(latest run {run_id})",
            reconciliation_run_id=run_id,
        )
        return HaltReason.RECON_INCONCLUSIVE_REPEAT


# ------------------------------------------------------------------- helpers


def external_order_payload(snap: BrokerOrderSnapshot) -> str:
    """The exact facts an acknowledgement of an external order is bound to."""
    return canonical_json({
        "broker_order_id": snap.broker_order_id, "client_order_id": snap.client_order_id,
        "symbol": snap.symbol, "side": snap.side.value, "qty": str(snap.qty),
        "filled_qty": str(snap.filled_qty), "status": snap.broker_status,
    })  # fmt: skip


def _outcome(findings: list[Finding]) -> Outcome:
    if any(f.severity is Severity.MISMATCH for f in findings):
        return Outcome.MISMATCH
    if any(f.severity is Severity.INCONCLUSIVE for f in findings):
        return Outcome.INCONCLUSIVE
    return Outcome.CLEAN


def lagging_symbol(f: Finding) -> str:
    return f.detail_dict().get("symbol", "")


def _ingest_findings(report: SyncReport) -> list[Finding]:
    from swing_trading_bot.ledger.portfolio import FillOutcome  # noqa: PLC0415

    conflicted = {aid for aid, o in report.fills.items() if o is FillOutcome.CONFLICT}
    conflicted |= set(report.conflicts)
    out = [
        finding(Category.ACTIVITY, "ACTIVITY_CONFLICT", Severity.MISMATCH, aid)
        for aid in sorted(conflicted)
    ]
    out += [
        finding(Category.EXECUTION, "OUT_OF_ORDER_FILL", Severity.MISMATCH, aid)
        for aid, outcome in report.fills.items()
        if outcome is FillOutcome.OUT_OF_ORDER
    ]
    if report.status is SyncStatus.INCOMPLETE:
        out += [
            finding(Category.RETRIEVAL, "SYNC_INCOMPLETE", Severity.INCONCLUSIVE, p)
            for p in report.problems
        ]
    return out


def _order_conflicts(
    mine: OrderRecord, snap: BrokerOrderSnapshot, *, is_open: bool, lagging: bool
) -> list[Finding]:
    out: list[Finding] = []
    coid = mine.client_order_id
    fields = {
        "symbol": (mine.symbol, snap.symbol),
        "side": (mine.side, snap.side),
        "qty": (mine.qty, snap.qty),
        "limit_price": (mine.limit_price, snap.limit_price),
        "order_type": (mine.order_type, snap.order_type),
    }
    if mine.broker_order_id is not None:
        fields["broker_order_id"] = (mine.broker_order_id, snap.broker_order_id)
    bad = {k: f"{a} vs {b}" for k, (a, b) in fields.items() if a != b}
    if bad:
        out.append(finding(Category.ORDER, "CONFLICTING_ORDER", Severity.MISMATCH, coid, **bad))
    if mine.status is OrderStatus.CONFIRMED_ABSENT:
        out.append(finding(Category.ORDER, "ABSENT_ORDER_EXISTS", Severity.MISMATCH, coid))
    elif mine.status in TERMINAL_STATUSES and is_open:
        out.append(
            finding(
                Category.ORDER,
                "TERMINAL_LOCALLY_OPEN_AT_BROKER",
                Severity.MISMATCH,
                coid,
                local=mine.status,
                broker=snap.broker_status,
            )
        )
    elif mine.filled_qty > snap.filled_qty:
        out.append(
            finding(
                Category.EXECUTION,
                "BOOKED_EXCEEDS_BROKER_FILLS",
                Severity.MISMATCH,
                coid,
                booked=mine.filled_qty,
                broker=snap.filled_qty,
            )
        )
    elif mine.status not in TERMINAL_STATUSES and not is_open and not lagging:
        # Broker terminal, fills complete, but the local order is still working: the
        # lifecycle should have applied it; report rather than guess.
        out.append(
            finding(
                Category.ORDER,
                "STATUS_NOT_APPLIED",
                Severity.INCONCLUSIVE,
                coid,
                local=mine.status,
                broker=snap.broker_status,
            )
        )
    return out


def _booking_order(s: Session) -> list[Finding]:
    """Fills booked in a different order from their execution (average cost then differs
    from execution order): needs an operator restatement."""
    out: list[Finding] = []
    rows = s.fetch_all("SELECT symbol, activity_id, filled_at FROM fills ORDER BY symbol, id")
    last: dict[str, str] = {}
    for symbol, aid, filled_at in rows:
        prev = last.get(str(symbol))
        if prev is not None and str(filled_at) < prev:
            out.append(
                finding(
                    Category.EXECUTION,
                    "OUT_OF_ORDER_BOOKING",
                    Severity.MISMATCH,
                    str(aid),
                    symbol=symbol,
                )
            )
        last[str(symbol)] = max(prev or "", str(filled_at))
    return out


# ------------------------------------------------- dependent-operation helpers


def latest_usable_run(
    s: Session, *, now: datetime, max_age: timedelta, after: datetime | None = None
) -> int | None:
    """The id of the latest run if it is CLEAN, not older than ``max_age`` and (when
    given) taken after ``after``; else None. Dependent operations re-check this at the
    moment they act, so evidence that has gone stale is never relied on."""
    run = ReconciliationRunsRepository().latest(s)
    if run is None or run.result is not ReconciliationResult.OK or run.id is None:
        return None
    if now - run.ts > max_age or run.ts > now or (after is not None and run.ts < after):
        return None
    return run.id


def resume_after_clean_reconciliation(
    db: Database, *, actor: str, detail: str, now: datetime, max_age: timedelta
) -> bool:
    """Operator resume (D11): only with a CLEAN run taken after the halt and still fresh.
    A refusal is recorded in ``halt_events``."""
    with db.transaction() as tx:
        state = TradingStateRepository().get(tx)
        run_id = latest_usable_run(tx, now=now, max_age=max_age, after=state.changed_at)
        repo = TradingStateRepository()
        if run_id is None:
            repo.refuse_resume(
                tx, reason_code="RECON_NOT_CLEAN_OR_STALE", detail=detail, actor=actor, at=now
            )
            return False
        return (
            repo.resume(tx, detail=detail, actor=actor, at=now, reconciliation_run_id=run_id).value
            == "RESUMED"
        )


def run_findings(s: Session, run_id: int) -> list[Finding]:
    rows = s.fetch_all(
        "SELECT category, code, severity, subject, detail_json "
        "FROM reconciliation_findings WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [
        Finding(
            Category(str(r[0])),
            str(r[1]),
            Severity(str(r[2])),
            str(r[3] or ""),
            tuple(sorted(json.loads(str(r[4])).items())) if r[4] else (),
        )
        for r in rows
    ]


__all__ = [
    "LAST_PERIODIC_RESCAN",
    "Category",
    "Finding",
    "Outcome",
    "ReconConfig",
    "Reconciler",
    "ReconciliationReport",
    "Severity",
    "latest_usable_run",
    "resume_after_clean_reconciliation",
    "run_findings",
]
