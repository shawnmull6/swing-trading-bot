"""Runtime wiring and the startup / operational sequences (ARCHITECTURE §10.1, §12.20).

Everything here reuses the existing M1 services; it adds only the orchestration and the
runtime state no earlier step wrote (recorded broker identity is written by
``init-ledger``; ``broker.last_successful_sync``, calendar refreshes and marks here).

Order of operations for a sync cycle (``sync`` and the operational commands):

1. preflight (broker reads only, outside any transaction): account, account
   configuration and clock; the connected account and environment must equal the
   identity recorded at initialization — a mismatch halts ``RECON_ACCOUNT`` **before**
   anything is ingested; an auth / malformed-payload failure halts per §10.5; any other
   read failure stops the cycle without a halt (transient);
2. calendar refresh, when the stored calendar is older than a day or too short;
3. recovery (``recover_after_restart``): forced lookups of unresolved orders, then
   activity and order synchronization, then escalation — never a send;
4. marks for held symbols (plus any explicitly requested symbols);
5. loss-limit evaluation (baselines, lockouts, halts);
6. ``broker.last_successful_sync`` is written only when the synchronization was OK.

No step clears a persistent halt; only :func:`resume` can, through the guarded
``resume_after_clean_reconciliation``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from swing_trading_bot.app.broker_factory import account_identity_problems
from swing_trading_bot.app.config import (
    CALENDAR_FETCH_AHEAD,
    CALENDAR_FETCH_BACK,
    CALENDAR_REFRESH_AFTER,
    CALENDAR_REQUIRED_AHEAD,
    AppConfig,
)
from swing_trading_bot.app.exit_codes import ExitCode, combine
from swing_trading_bot.broker.errors import BrokerError
from swing_trading_bot.broker.models import (
    AccountConfigSnapshot,
    AccountSnapshot,
    ActivityPage,
    MarketClock,
)
from swing_trading_bot.broker.ports import (
    MAX_ACTIVITY_PAGE_SIZE,
    BrokerReadPort,
    PriceMarkSource,
)
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarFetch, CalendarKind
from swing_trading_bot.domain.enums import HaltReason, OrderStatus, RuntimeMode
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition, PriceMark
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES
from swing_trading_bot.ledger.broker_outage import (
    OutageEvent,
    OutageUpdate,
    load_streak,
    record_complete_cycle,
    record_failed_cycle,
)
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.codec import date_from_db, ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Database, Session, assert_no_transaction
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor, LossLimitReport
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    PositionsRepository,
    SyncStateRepository,
    TradingState,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import (
    BROKER_ACCOUNT_ID,
    BROKER_ENVIRONMENT,
    LAST_SUCCESSFUL_SYNC,
)
from swing_trading_bot.oms.lifecycle import read_error_halt, read_error_outage_category
from swing_trading_bot.oms.order_manager import CrashHook
from swing_trading_bot.oms.rebaseline import Decision, RebaselineOutcome, Rebaseliner
from swing_trading_bot.oms.reconciler import (
    Outcome,
    Reconciler,
    ReconciliationReport,
    resume_after_clean_reconciliation,
)
from swing_trading_bot.oms.recovery import (
    AbsenceEvidence,
    AmbiguousOrderRecovery,
    recover_after_restart,
)
from swing_trading_bot.oms.sync import OrderSync, SyncReport, SyncStatus
from swing_trading_bot.risk.gates import EXPECTED_ENVIRONMENT, GateStatus, TradingGates

_log = logging.getLogger(__name__)
RUNTIME_ACTOR = "runtime"
Clock = Callable[[], datetime]


class ObserveBroker(BrokerReadPort, PriceMarkSource, Protocol):
    """What the runtime reads from: the read port plus price marks. No execution."""


class RecordingBroker:
    """Read-only pass-through that remembers every broker error a read raised, so one
    sync cycle can be classified once for the outage breaker however many requests
    failed (and whichever service caught the error). It has no write methods."""

    def __init__(self, inner: ObserveBroker) -> None:
        self.inner = inner
        self.failures: list[BrokerError] = []

    def reset(self) -> None:
        self.failures = []

    def _call[T](self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except BrokerError as exc:
            self.failures.append(exc)
            raise

    def get_account(self) -> AccountSnapshot:
        return self._call(self.inner.get_account)

    def get_account_config(self) -> AccountConfigSnapshot:
        return self._call(self.inner.get_account_config)

    def get_clock(self) -> MarketClock:
        return self._call(self.inner.get_clock)

    def get_calendar(self, kind: CalendarKind, start: date, end: date) -> CalendarFetch:
        return self._call(lambda: self.inner.get_calendar(kind, start, end))

    def get_positions(self) -> list[BrokerPosition]:
        return self._call(self.inner.get_positions)

    def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        return self._call(self.inner.get_open_orders)

    def get_orders(
        self, *, after: datetime, until: datetime | None = None
    ) -> list[BrokerOrderSnapshot]:
        return self._call(lambda: self.inner.get_orders(after=after, until=until))

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        return self._call(lambda: self.inner.get_order_by_client_id(client_order_id))

    def get_activities(
        self,
        *,
        page_token: str | None,
        page_size: int = MAX_ACTIVITY_PAGE_SIZE,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> ActivityPage:
        return self._call(
            lambda: self.inner.get_activities(
                page_token=page_token, page_size=page_size, after=after, until=until
            )
        )

    def get_marks(self, symbols: Sequence[str]) -> Mapping[str, PriceMark]:
        return self._call(lambda: self.inner.get_marks(symbols))


@dataclass(slots=True)
class Runtime:
    mode: RuntimeMode
    db: Database
    broker: RecordingBroker
    config: AppConfig
    clock: Clock
    pf: VirtualPortfolio
    monitor: LossLimitMonitor
    gates: TradingGates
    sync: OrderSync
    recovery: AmbiguousOrderRecovery
    reconciler: Reconciler
    rebaseliner: Rebaseliner

    def now(self) -> datetime:
        return self.clock()

    def state(self) -> TradingState:
        with self.db.read() as s:
            return TradingStateRepository().get(s)


def build_runtime(
    db: Database,
    source: ObserveBroker,
    config: AppConfig,
    *,
    clock: Clock,
    trading_enabled: bool,
    crash: CrashHook | None = None,
) -> Runtime:
    """Wire the existing services around one recording read port. Never builds an
    execution capability."""
    broker = RecordingBroker(source)
    mode = db.identity.mode
    pf = VirtualPortfolio(db, config.portfolio())
    monitor = LossLimitMonitor(db, pf, config.loss_limits(mode))
    gates = TradingGates(
        db, pf, monitor, broker, config.gates(mode, trading_enabled=trading_enabled)
    )
    oms = config.oms(mode)
    reconciler = Reconciler(db, pf, broker, config.reconciliation(mode), crash=crash)
    return Runtime(
        mode=mode,
        db=db,
        broker=broker,
        config=config,
        clock=clock,
        pf=pf,
        monitor=monitor,
        gates=gates,
        sync=OrderSync(db, pf, broker, oms, crash=crash),
        recovery=AmbiguousOrderRecovery(db, pf, broker, oms),
        reconciler=reconciler,
        rebaseliner=Rebaseliner(db, pf, reconciler, crash=crash),
    )


# ===================================================================== helpers
def halt_once(db: Database, reason: HaltReason, detail: str, *, at: datetime) -> bool:
    """Set a persistent halt unless the ledger is already halted for the same reason.
    Returns True when a halt was written."""
    with db.transaction() as tx:
        repo = TradingStateRepository()
        state = repo.get(tx)
        if state.halted and state.reason_code == reason.value:
            return False
        prior = f" (previously halted: {state.reason_code})" if state.halted else ""
        repo.halt(tx, reason, detail=(detail + prior)[:2000], actor=RUNTIME_ACTOR, at=at)
    _log.warning("persistent halt", extra={"halt_reason": reason.value, "detail": detail})
    return True


def new_halt(before: TradingState, after: TradingState) -> bool:
    return after.halted and (
        not before.halted
        or after.changed_at != before.changed_at
        or after.reason_code != before.reason_code
    )


def state_dict(state: TradingState) -> dict[str, Any]:
    return {
        "halted": state.halted,
        "reason_code": state.reason_code,
        "reason_detail": state.reason_detail,
        "changed_at": state.changed_at.isoformat(),
        "changed_by": state.changed_by,
    }


# ===================================================================== preflight
@dataclass(slots=True)
class PreflightReport:
    account: AccountSnapshot | None = None
    account_config: AccountConfigSnapshot | None = None
    clock: MarketClock | None = None
    broker_error: str | None = None
    identity_problem: str | None = None
    identity_mismatch: bool = False
    halted: HaltReason | None = None

    @property
    def ok(self) -> bool:
        return self.broker_error is None and self.identity_problem is None

    @property
    def exit_code(self) -> ExitCode:
        if self.halted is not None or self.identity_mismatch:
            return ExitCode.HALT_OR_MISMATCH
        if self.broker_error is not None:
            return ExitCode.BROKER_UNAVAILABLE
        if self.identity_problem is not None:
            return ExitCode.REFUSED
        return ExitCode.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "account_id": None if self.account is None else self.account.account_id,
            "environment": None if self.account is None else self.account.environment,
            "market_open": None if self.clock is None else self.clock.is_open,
            "m2_account_protections": None
            if self.account_config is None
            else self.account_config.m2_protections_in_place,
            "broker_error": self.broker_error,
            "identity_problem": self.identity_problem,
            "halted": None if self.halted is None else self.halted.value,
        }


def preflight(rt: Runtime) -> PreflightReport:
    """§10.1 step 4: read-only broker preflight and identity verification."""
    assert_no_transaction("preflight")
    report = PreflightReport()
    now = rt.now()
    try:
        report.account = rt.broker.get_account()
        report.account_config = rt.broker.get_account_config()
        report.clock = rt.broker.get_clock()
    except BrokerError as exc:
        report.broker_error = f"{type(exc).__name__}: {exc}"
        reason = read_error_halt(exc)
        if reason is not None and halt_once(
            rt.db, reason, f"preflight read failed: {report.broker_error}", at=now
        ):
            report.halted = reason
        return report
    with rt.db.read() as s:
        sync = SyncStateRepository()
        recorded_id = sync.get(s, BROKER_ACCOUNT_ID)
        recorded_env = sync.get(s, BROKER_ENVIRONMENT)
    if recorded_id is None or recorded_env is None:
        report.identity_problem = "broker identity was never recorded (ledger not initialized)"
        return report
    account = report.account
    uncertain = account_identity_problems(account, environment=EXPECTED_ENVIRONMENT[rt.mode])
    if uncertain:
        report.identity_problem = "connected account identity is uncertain: " + "; ".join(uncertain)
        report.identity_mismatch = True
        if halt_once(rt.db, HaltReason.RECON_ACCOUNT, report.identity_problem, at=now):
            report.halted = HaltReason.RECON_ACCOUNT
        return report
    if account.account_id != recorded_id or account.environment != recorded_env:
        report.identity_problem = (
            f"connected account {account.account_id}/{account.environment} differs from the "
            f"recorded {recorded_id}/{recorded_env}"
        )
        report.identity_mismatch = True
        if halt_once(rt.db, HaltReason.RECON_ACCOUNT, report.identity_problem, at=now):
            report.halted = HaltReason.RECON_ACCOUNT
    return report


# ===================================================================== calendar
@dataclass(slots=True)
class CalendarRefresh:
    attempted: bool = False
    results: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "results": dict(self.results),
            "problems": list(self.problems),
            "error": self.error,
        }


def calendar_refresh_due(s: Session, now: datetime) -> bool:
    today = now.astimezone(MARKET_TZ).date()
    for kind in CalendarKind:
        row = s.fetch_one(
            "SELECT max(fetched_at), max(range_end) FROM calendar_fetches "
            "WHERE status = 'ACCEPTED' AND kind = ?",
            (kind.value,),
        )
        if row is None or row[0] is None or row[1] is None:
            return True
        if now - ts_from_db(row[0]) >= CALENDAR_REFRESH_AFTER:
            return True
        if date_from_db(row[1]) < today + CALENDAR_REQUIRED_AHEAD:
            return True
    return False


def refresh_calendar(rt: Runtime, *, force: bool = False) -> CalendarRefresh:
    """Fetch both calendars from the broker and record them (rejections are audited by
    the store and simply never become coverage)."""
    out = CalendarRefresh()
    now = rt.now()
    with rt.db.read() as s:
        if not force and not calendar_refresh_due(s, now):
            return out
    out.attempted = True
    today = now.astimezone(MARKET_TZ).date()
    start, end = today - CALENDAR_FETCH_BACK, today + CALENDAR_FETCH_AHEAD
    try:
        fetches = [rt.broker.get_calendar(kind, start, end) for kind in CalendarKind]
    except BrokerError as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    store = CalendarStore()
    with rt.db.transaction() as tx:
        for fetch in fetches:
            result = store.record_fetch(tx, fetch)
            out.results[fetch.kind.value] = result.status.value
            out.problems.extend(result.problems)
    return out


# ===================================================================== marks
@dataclass(slots=True)
class MarkRefresh:
    requested: tuple[str, ...] = ()
    recorded: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        missing = sorted(set(self.requested) - set(self.recorded))
        return {"requested": list(self.requested), "recorded": list(self.recorded),
                "missing": missing, "error": self.error}  # fmt: skip


def held_symbols(rt: Runtime) -> tuple[str, ...]:
    with rt.db.read() as s:
        pid = rt.pf.portfolio(s).id or 0
        return tuple(
            sorted(p.symbol for p in PositionsRepository().list_for_portfolio(s, pid) if p.qty)
        )


def record_marks(rt: Runtime, extra: Iterable[str] = ()) -> MarkRefresh:
    symbols = tuple(sorted({*held_symbols(rt), *extra}))
    out = MarkRefresh(requested=symbols)
    if not symbols:
        return out
    try:
        marks = rt.broker.get_marks(symbols)
    except BrokerError as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    with rt.db.transaction() as tx:
        for mark in marks.values():
            rt.pf.record_mark(tx, mark)
    out.recorded = tuple(sorted(marks))
    return out


# ===================================================================== sync cycle
@dataclass(slots=True)
class CycleReport:
    before: TradingState
    after: TradingState
    preflight: PreflightReport
    calendar: CalendarRefresh | None = None
    looked_up: dict[str, str] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)
    sync: SyncReport | None = None
    marks: MarkRefresh | None = None
    loss: LossLimitReport | None = None
    last_successful_sync: datetime | None = None
    outage: OutageUpdate | None = None
    failure_categories: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return self.preflight.ok and self.sync is not None

    @property
    def exit_code(self) -> ExitCode:
        codes = [self.preflight.exit_code]
        if new_halt(self.before, self.after):
            codes.append(ExitCode.HALT_OR_MISMATCH)
        if self.sync is not None and self.sync.status is not SyncStatus.OK:
            codes.append(
                ExitCode.HALT_OR_MISMATCH
                if self.sync.status is SyncStatus.HALTED
                else ExitCode.INCONCLUSIVE
            )
        if self.failure_categories:
            codes.append(ExitCode.BROKER_UNAVAILABLE)
        if self.preflight.ok and not cycle_complete(self):
            codes.append(ExitCode.INCONCLUSIVE)
        return combine(*codes)

    def to_dict(self) -> dict[str, Any]:
        sync = self.sync
        return {
            "preflight": self.preflight.to_dict(),
            "calendar": None if self.calendar is None else self.calendar.to_dict(),
            "recovery": {"looked_up": dict(self.looked_up), "escalated": list(self.escalated)},
            "sync": None
            if sync is None
            else {
                "status": sync.status.value,
                "fills": {k: v.value for k, v in sync.fills.items()},
                "cash_activities": sync.cash_activities,
                "unclassified": list(sync.unclassified),
                "adopted": list(sync.adopted),
                "transitions": {k: v.value for k, v in sync.transitions.items()},
                "coverage_advanced": sync.coverage_advanced,
                "problems": list(sync.problems),
            },
            "marks": None if self.marks is None else self.marks.to_dict(),
            "loss_limits": None
            if self.loss is None
            else {
                "blockers": [b.value for b in self.loss.blockers],
                "lockout_active": self.loss.lockout_active,
                "weekly_breach_active": self.loss.weekly_breach_active,
                "problems": list(self.loss.problems),
            },
            "last_successful_sync": None
            if self.last_successful_sync is None
            else self.last_successful_sync.isoformat(),
            "broker_outage": None
            if self.outage is None
            else {**self.outage.to_dict(), "cycle_failures": list(self.failure_categories)},
            "new_halt": new_halt(self.before, self.after),
            "trading_state": state_dict(self.after),
        }


def sync_cycle(rt: Runtime, *, extra_marks: Iterable[str] = ()) -> CycleReport:
    """§10.1 steps 4-7 (see the module docstring for the exact order), then the
    broker-outage breaker update for this cycle (§12.21)."""
    started = rt.now()
    rt.broker.reset()
    before = rt.state()
    pre = preflight(rt)
    report = CycleReport(before=before, after=before, preflight=pre)
    if pre.ok:
        _run_cycle(rt, report, extra_marks)
    _update_outage(rt, report, cycle_at=started)
    report.after = rt.state()
    return report


def _run_cycle(rt: Runtime, report: CycleReport, extra_marks: Iterable[str]) -> None:
    report.calendar = refresh_calendar(rt)
    now = rt.now()

    recovery, synced = recover_after_restart(rt.recovery, rt.sync, now=now)
    report.looked_up = {k: v.value for k, v in recovery.looked_up.items()}
    report.escalated = list(recovery.escalated)
    report.sync = synced
    report.marks = record_marks(rt, extra_marks)
    report.loss = rt.monitor.evaluate(now=now)
    if synced.status is SyncStatus.OK:
        with rt.db.transaction() as tx:
            SyncStateRepository().set(tx, LAST_SUCCESSFUL_SYNC, ts_to_db(now), now)
        report.last_successful_sync = now


def cycle_complete(report: CycleReport) -> bool:
    """Every broker-dependent step of the cycle completed (a partial sync does not)."""
    return (
        report.preflight.ok
        and report.sync is not None
        and report.sync.status is SyncStatus.OK
        and (report.calendar is None or report.calendar.error is None)
        and (report.marks is None or report.marks.error is None)
    )


def _update_outage(rt: Runtime, report: CycleReport, *, cycle_at: datetime) -> None:
    """One breaker update per cycle: a failed cycle (any qualifying broker failure) is
    recorded once; a complete cycle resets the streak; anything else (auth, identity,
    payload, ledger problems, partial completion) leaves the streak untouched."""
    categories = tuple(
        c for c in (read_error_outage_category(e) for e in rt.broker.failures) if c is not None
    )
    report.failure_categories = categories
    with rt.db.transaction() as tx:
        if categories:
            report.outage = record_failed_cycle(
                tx, cycle_at=cycle_at, category=categories[0], policy=rt.config.outage()
            )
        elif cycle_complete(report):
            report.outage = record_complete_cycle(tx, cycle_at=cycle_at)
        else:
            report.outage = OutageUpdate(OutageEvent.UNCHANGED, load_streak(tx))
    if report.outage.escalated:
        _log.error("persistent halt", extra={"halt_reason": "BROKER_OUTAGE"})


# ===================================================================== reconcile
RECON_EXIT: dict[Outcome, ExitCode] = {
    Outcome.CLEAN: ExitCode.OK,
    Outcome.INCONCLUSIVE: ExitCode.INCONCLUSIVE,
    Outcome.MISMATCH: ExitCode.HALT_OR_MISMATCH,
}


def recon_dict(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "outcome": report.outcome.value,
        "halted": None if report.halted is None else report.halted.value,
        "findings": [
            {
                "severity": f.severity.value,
                "category": f.category.value,
                "code": f.code,
                "subject": f.subject,
                "detail": f.detail_dict(),
            }
            for f in report.findings
        ],
    }


@dataclass(slots=True)
class ReconcileResult:
    cycle: CycleReport
    recon: ReconciliationReport | None = None
    gate: GateStatus | None = None

    @property
    def exit_code(self) -> ExitCode:
        codes = [self.cycle.exit_code]
        if self.recon is None:
            codes.append(ExitCode.REFUSED)
        else:
            codes.append(RECON_EXIT[self.recon.outcome])
        return combine(*codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle.to_dict(),
            "reconciliation": None if self.recon is None else recon_dict(self.recon),
            "gates": None if self.gate is None else self.gate.to_dict(),
        }


def reconcile(rt: Runtime) -> ReconcileResult:
    """Full startup (§10.1 1-8): sync cycle, reconciliation, startup readiness."""
    cycle = sync_cycle(rt)
    result = ReconcileResult(cycle)
    if not cycle.preflight.ok:
        return result
    result.recon = rt.reconciler.run(now=rt.now())
    result.gate = rt.gates.run_startup(now=rt.now())
    result.cycle.after = rt.state()
    return result


# ===================================================================== operator ops
@dataclass(slots=True)
class ResumeResult:
    reconcile: ReconcileResult
    was_halted: bool
    resumed: bool

    @property
    def exit_code(self) -> ExitCode:
        if not self.was_halted:
            return combine(self.reconcile.cycle.preflight.exit_code)
        if self.resumed:
            return ExitCode.OK
        return combine(self.reconcile.exit_code, ExitCode.REFUSED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "was_halted": self.was_halted,
            "resumed": self.resumed,
            **self.reconcile.to_dict(),
        }


def resume(rt: Runtime, *, actor: str, reason: str) -> ResumeResult:
    """D11: run a full reconciliation, then resume only through the guarded path that
    requires a CLEAN run taken after the halt (refusals are audited)."""
    was_halted = rt.state().halted
    result = reconcile(rt)
    if not was_halted:
        return ResumeResult(result, was_halted=False, resumed=False)
    resumed = False
    if result.cycle.preflight.ok:
        resumed = resume_after_clean_reconciliation(
            rt.db,
            actor=actor,
            detail=reason,
            now=rt.now(),
            max_age=rt.config.reconciliation(rt.mode).clean_max_age,
        )
    result.cycle.after = rt.state()
    return ResumeResult(result, was_halted=True, resumed=resumed)


def operator_halt(db: Database, *, actor: str, reason: str, at: datetime) -> TradingState:
    with db.transaction() as tx:
        repo = TradingStateRepository()
        prior = repo.get(tx)
        detail = reason if not prior.halted else f"{reason} (previously: {prior.reason_code})"
        repo.halt(tx, HaltReason.OPERATOR, detail=detail[:2000], actor=actor, at=at)
        return repo.get(tx)


@dataclass(slots=True)
class ResolveResult:
    preflight: PreflightReport
    client_order_id: str
    problem: str | None = None
    lookup: str | None = None
    list_scan: str | None = None
    activity_scan: str | None = None
    recon: ReconciliationReport | None = None
    evidence: AbsenceEvidence | None = None
    status_after: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.evidence is not None and self.evidence.complete

    @property
    def exit_code(self) -> ExitCode:
        if not self.preflight.ok:
            return self.preflight.exit_code
        return ExitCode.OK if self.confirmed else ExitCode.REFUSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "preflight": self.preflight.to_dict(),
            "problem": self.problem,
            "lookup": self.lookup,
            "list_scan": self.list_scan,
            "activity_scan": self.activity_scan,
            "reconciliation": None if self.recon is None else recon_dict(self.recon),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "confirmed_absent": self.confirmed,
            "status_after": self.status_after,
        }


def resolve_order(rt: Runtime, coid: str, *, actor: str) -> ResolveResult:
    """U4 operator confirmation. Order: preflight → one lookup → list scan → activity
    scan → fresh reconciliation → ``confirm_absent`` (which refuses, and records the
    refusal, unless every piece of §10.3 evidence is present). Never sends anything."""
    pre = preflight(rt)
    out = ResolveResult(preflight=pre, client_order_id=coid)
    if not pre.ok:
        return out
    with rt.db.read() as s:
        order = OrdersRepository().get(s, coid)
    if order is None:
        out.problem = "no such order in this ledger"
        return out
    if order.status not in AMBIGUOUS_STATUSES:
        out.problem = f"order is {order.status.value}, not an unresolved submission"
        return out
    status = rt.recovery.lookup(coid, now=rt.now())
    out.lookup = status.value
    if status in AMBIGUOUS_STATUSES:
        out.list_scan = rt.recovery.list_scan(coid, now=rt.now()).value
        out.activity_scan = rt.recovery.activity_scan(coid, now=rt.now()).value
        out.recon = rt.reconciler.run(now=rt.now())
        out.evidence = rt.recovery.confirm_absent(coid, operator=actor, now=rt.now())
    else:
        out.problem = f"the broker knows this order (now {out.lookup}); nothing to confirm"
    with rt.db.read() as s:
        after = OrdersRepository().get(s, coid)
    out.status_after = None if after is None else after.status.value
    return out


@dataclass(slots=True)
class RebaselineResult:
    preflight: PreflightReport
    outcome: RebaselineOutcome | None = None

    @property
    def exit_code(self) -> ExitCode:
        if not self.preflight.ok:
            return self.preflight.exit_code
        if self.outcome is None:
            return ExitCode.REFUSED
        ok = {Decision.PROPOSED, Decision.APPROVED, Decision.DUPLICATE}
        return ExitCode.OK if self.outcome.decision in ok else ExitCode.REFUSED

    def to_dict(self) -> dict[str, Any]:
        o = self.outcome
        return {
            "preflight": self.preflight.to_dict(),
            "decision": None if o is None else o.decision.value,
            "request_id": None if o is None else o.request_id,
            "reasons": [] if o is None else list(o.reasons),
            "unexplained_amount_to_acknowledge": None
            if o is None or o.unexplained is None
            else str(o.unexplained),
            "new_baseline_id": None if o is None else o.new_baseline_id,
            "reconciliation_run_id": None if o is None else o.reconciliation_run_id,
            "preview": None if o is None or o.preview is None else o.preview.as_dict(),
        }


def rebaseline_propose(rt: Runtime, *, actor: str, reason: str) -> RebaselineResult:
    """Order: preflight → ``Rebaseliner.propose`` (which runs its own reconciliation).
    No separate sync cycle, so nothing else moves the evidence the proposal binds."""
    pre = preflight(rt)
    out = RebaselineResult(pre)
    if pre.ok:
        out.outcome = rt.rebaseliner.propose(actor=actor, reason=reason, now=rt.now())
    return out


def rebaseline_approve(
    rt: Runtime, request_id: int, *, acknowledged: str, actor: str, reason: str
) -> RebaselineResult:
    """Order: preflight → ``Rebaseliner.confirm`` (second fresh reconciliation inside).
    The halt remains; ``resume`` is still required."""
    pre = preflight(rt)
    out = RebaselineResult(pre)
    if pre.ok:
        out.outcome = rt.rebaseliner.confirm(
            request_id, acknowledged=acknowledged, actor=actor, reason=reason, now=rt.now()
        )
    return out


def unresolved_orders(s: Session) -> list[str]:
    return [
        o.client_order_id
        for o in OrdersRepository().list_working(s)
        if o.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMIT_AMBIGUOUS)
    ]


def as_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
