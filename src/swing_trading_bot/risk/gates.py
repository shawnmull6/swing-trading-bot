"""Startup readiness and trading gates (ARCHITECTURE §7, §10.1, M1 step 10).

Three layers, all evaluated on every call, all of which must be clear before a **new
entry** is eligible:

* ``STARTUP`` — readiness prerequisites (ledger mode/integrity/invariants, portfolio,
  recorded broker identity and environment, supported account configuration, clean
  recent reconciliation). The in-memory startup gate is set at process start and is
  cleared by :meth:`TradingGates.run_startup` only when every prerequisite passes.
  Clearing it removes ``STARTUP_INCOMPLETE`` and nothing else.
* ``PERSISTENT`` — the stored halt. Never cleared here; only an operator resume backed by
  a clean reconciliation (``TradingStateRepository.resume``) clears it.
* ``TRANSIENT`` — conditions re-derived each time: broker reads, market hours, sync and
  activity-coverage freshness, calendar coverage, marks, unresolved orders, unexplained
  activities, settlement, capital, loss limits, gap stress, trading switch, runtime mode.

Evaluation is **read-only** and **fails closed**: any missing, stale, contradictory or
unavailable input produces a blocking finding; an unknown upstream blocker code maps to
``UNMAPPED_BLOCKER``. Broker reads happen before (never inside) the ledger snapshot.

Findings block *new entries*. Risk-reducing exits are reported separately: halts and loss
limits never forbid them in principle; only conditions that would make any broker
action unsafe (broker unreachable, wrong account/environment, ledger damage) do. Exit
execution itself does not exist in M1 (``exit_execution_available`` is False).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
)
from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot, MarketClock
from swing_trading_bot.broker.ports import BrokerReadPort
from swing_trading_bot.domain.calendar import CalendarError
from swing_trading_bot.domain.enums import ActivityAttribution, OrderStatus, RuntimeMode
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.acknowledgements import ACKNOWLEDGED_ACTIVITY_SQL
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.broker_outage import load_streak
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.codec import ts_from_db
from swing_trading_bot.ledger.db import Database, Session, assert_no_transaction
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio, market_date
from swing_trading_bot.ledger.repositories import (
    ReconciliationRunsRepository,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import (
    BROKER_ACCOUNT_ID,
    BROKER_ENVIRONMENT,
    LAST_SUCCESSFUL_SYNC,
)

EXPECTED_ENVIRONMENT: Final = {RuntimeMode.OBSERVE: "paper", RuntimeMode.SIMULATE: "sim"}
UNRESOLVED_ORDER_STATUSES: Final = (
    OrderStatus.PENDING_SUBMIT,
    OrderStatus.SUBMIT_AMBIGUOUS,
    OrderStatus.ORPHAN,
)


class GateLayer(StrEnum):
    STARTUP = "STARTUP"
    PERSISTENT = "PERSISTENT"
    TRANSIENT = "TRANSIENT"
    ADVISORY = "ADVISORY"


_LAYER_ORDER: Final = {
    GateLayer.STARTUP: 0,
    GateLayer.PERSISTENT: 1,
    GateLayer.TRANSIENT: 2,
    GateLayer.ADVISORY: 3,
}


class GateReason(StrEnum):
    # startup prerequisites
    STARTUP_INCOMPLETE = "STARTUP_INCOMPLETE"
    LEDGER_MODE_MISMATCH = "LEDGER_MODE_MISMATCH"
    LEDGER_INTEGRITY_FAILED = "LEDGER_INTEGRITY_FAILED"
    LEDGER_INVARIANT_VIOLATED = "LEDGER_INVARIANT_VIOLATED"
    PORTFOLIO_MISSING = "PORTFOLIO_MISSING"
    BROKER_IDENTITY_UNRECORDED = "BROKER_IDENTITY_UNRECORDED"
    BROKER_IDENTITY_MISMATCH = "BROKER_IDENTITY_MISMATCH"
    BROKER_ENVIRONMENT_MISMATCH = "BROKER_ENVIRONMENT_MISMATCH"
    BROKER_ACCOUNT_CONFIG_UNSUPPORTED = "BROKER_ACCOUNT_CONFIG_UNSUPPORTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    # persistent
    PERSISTENT_HALT = "PERSISTENT_HALT"
    # transient
    RUNTIME_MODE_NO_EXECUTION = "RUNTIME_MODE_NO_EXECUTION"
    TRADING_DISABLED = "TRADING_DISABLED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    BROKER_AUTH_FAILED = "BROKER_AUTH_FAILED"
    BROKER_DATA_INVALID = "BROKER_DATA_INVALID"
    BROKER_ACCOUNT_RESTRICTED = "BROKER_ACCOUNT_RESTRICTED"
    MARKET_CLOSED = "MARKET_CLOSED"
    BROKER_SYNC_STALE = "BROKER_SYNC_STALE"
    BROKER_OUTAGE_ACTIVE = "BROKER_OUTAGE_ACTIVE"
    ACTIVITY_COVERAGE_INCOMPLETE = "ACTIVITY_COVERAGE_INCOMPLETE"
    RECONCILIATION_STALE = "RECONCILIATION_STALE"
    RECONCILIATION_INCONCLUSIVE = "RECONCILIATION_INCONCLUSIVE"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    CALENDAR_COVERAGE_INSUFFICIENT = "CALENDAR_COVERAGE_INSUFFICIENT"
    UNRESOLVED_ORDERS = "UNRESOLVED_ORDERS"
    UNEXPLAINED_ACTIVITY = "UNEXPLAINED_ACTIVITY"
    UNBOOKED_FILLS = "UNBOOKED_FILLS"
    MARKS_STALE_OR_MISSING = "MARKS_STALE_OR_MISSING"
    UNRESOLVED_SETTLEMENT = "UNRESOLVED_SETTLEMENT"
    NEGATIVE_AVAILABLE_CASH = "NEGATIVE_AVAILABLE_CASH"
    CAPITAL_CEILING_EXCEEDED = "CAPITAL_CEILING_EXCEEDED"
    NEGATIVE_POSITION = "NEGATIVE_POSITION"
    NO_BUYING_CAPACITY = "NO_BUYING_CAPACITY"
    SESSION_BASELINE_MISSING = "SESSION_BASELINE_MISSING"
    WEEK_BASELINE_MISSING = "WEEK_BASELINE_MISSING"
    EQUITY_UNKNOWN = "EQUITY_UNKNOWN"
    DAILY_LOSS_LOCKOUT = "DAILY_LOSS_LOCKOUT"
    WEEKLY_LIMIT_ACTIVE = "WEEKLY_LIMIT_ACTIVE"
    GAP_STRESS_PORTFOLIO_EXCEEDED = "GAP_STRESS_PORTFOLIO_EXCEEDED"
    UNMAPPED_BLOCKER = "UNMAPPED_BLOCKER"
    # advisories (non-blocking in M1)
    ACCOUNT_PROTECTIONS_NOT_SET = "ACCOUNT_PROTECTIONS_NOT_SET"
    CALENDAR_HORIZON_SHORT = "CALENDAR_HORIZON_SHORT"


_UPSTREAM: Final[dict[str, GateReason]] = {
    "STALE_OR_MISSING_MARKS": GateReason.MARKS_STALE_OR_MISSING,
    "UNRESOLVED_SETTLEMENT": GateReason.UNRESOLVED_SETTLEMENT,
    "NEGATIVE_AVAILABLE_CASH": GateReason.NEGATIVE_AVAILABLE_CASH,
    "CAPITAL_CEILING_EXCEEDED": GateReason.CAPITAL_CEILING_EXCEEDED,
    "NEGATIVE_POSITION": GateReason.NEGATIVE_POSITION,
    "NO_BUYING_CAPACITY": GateReason.NO_BUYING_CAPACITY,
    "CALENDAR_UNAVAILABLE": GateReason.CALENDAR_UNAVAILABLE,
    "SESSION_BASELINE_MISSING": GateReason.SESSION_BASELINE_MISSING,
    "WEEK_BASELINE_MISSING": GateReason.WEEK_BASELINE_MISSING,
    "EQUITY_UNKNOWN": GateReason.EQUITY_UNKNOWN,
    "DAILY_LOSS_LOCKOUT": GateReason.DAILY_LOSS_LOCKOUT,
    "WEEKLY_LIMIT_ACTIVE": GateReason.WEEKLY_LIMIT_ACTIVE,
    "GAP_STRESS_PORTFOLIO_EXCEEDED": GateReason.GAP_STRESS_PORTFOLIO_EXCEEDED,
}

EXIT_BLOCKING: Final = frozenset(
    {
        GateReason.LEDGER_MODE_MISMATCH,
        GateReason.LEDGER_INTEGRITY_FAILED,
        GateReason.PORTFOLIO_MISSING,
        GateReason.BROKER_IDENTITY_UNRECORDED,
        GateReason.BROKER_IDENTITY_MISMATCH,
        GateReason.BROKER_ENVIRONMENT_MISMATCH,
        GateReason.BROKER_UNAVAILABLE,
        GateReason.BROKER_AUTH_FAILED,
        GateReason.BROKER_DATA_INVALID,
    }
)
"""Conditions under which even a risk-reducing exit could not be done safely."""


@dataclass(frozen=True, slots=True, order=True)
class GateFinding:
    sort_key: tuple[int, str, str] = field(init=False, repr=False)
    layer: GateLayer
    reason: GateReason
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sort_key", (_LAYER_ORDER[self.layer], self.reason.value, self.detail)
        )

    @property
    def blocking(self) -> bool:
        return self.layer is not GateLayer.ADVISORY


@dataclass(frozen=True, slots=True)
class ExitStatus:
    permitted_in_principle: bool
    blocking_reasons: tuple[GateReason, ...]
    execution_available: bool = False  # no exit execution exists in M1


@dataclass(frozen=True, slots=True)
class GateStatus:
    evaluated_at: datetime
    mode: RuntimeMode
    startup_complete: bool
    findings: tuple[GateFinding, ...]

    @property
    def blocking(self) -> tuple[GateFinding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    @property
    def advisories(self) -> tuple[GateFinding, ...]:
        return tuple(f for f in self.findings if not f.blocking)

    @property
    def new_entries_allowed(self) -> bool:
        return not self.blocking

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(sorted({f.reason.value for f in self.blocking}))

    @property
    def persistent_halt(self) -> bool:
        return any(f.reason is GateReason.PERSISTENT_HALT for f in self.findings)

    @property
    def exits(self) -> ExitStatus:
        reasons = tuple(sorted({f.reason for f in self.blocking if f.reason in EXIT_BLOCKING}))
        return ExitStatus(permitted_in_principle=not reasons, blocking_reasons=reasons)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-friendly operator report."""
        exits = self.exits
        return {
            "evaluated_at": self.evaluated_at.isoformat(),
            "mode": self.mode.value,
            "startup_complete": self.startup_complete,
            "new_entries_allowed": self.new_entries_allowed,
            "persistent_halt": self.persistent_halt,
            "reason_codes": list(self.reason_codes),
            "findings": [
                {"layer": f.layer.value, "reason": f.reason.value, "detail": f.detail}
                for f in self.findings
            ],
            "risk_reducing_exits": {
                "permitted_in_principle": exits.permitted_in_principle,
                "blocking_reasons": [r.value for r in exits.blocking_reasons],
                "execution_available": exits.execution_available,
            },
        }

    def render(self) -> str:
        head = "NEW ENTRIES ALLOWED" if self.new_entries_allowed else "NEW ENTRIES BLOCKED"
        lines = [
            f"{head} ({self.mode.value}, {self.evaluated_at.isoformat()})",
            f"startup complete: {self.startup_complete}",
        ]
        lines += [f"  [{f.layer.value}] {f.reason.value}: {f.detail}" for f in self.findings]
        exits = self.exits
        lines.append(
            "risk-reducing exits: "
            + (
                "permitted in principle"
                if exits.permitted_in_principle
                else "NOT safe: " + ", ".join(r.value for r in exits.blocking_reasons)
            )
            + " (exit execution not implemented in M1)"
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class GateConfig:
    mode: RuntimeMode
    trading_enabled: bool
    max_sync_age: timedelta
    calendar_max_age: timedelta
    calendar_coverage_days: int
    calendar_horizon_warning_days: int
    activity_posting_lag: timedelta
    reconciliation_max_age: timedelta
    require_m2_account_protections: bool = False  # U6: report-only in M1

    def __post_init__(self) -> None:
        if self.mode is RuntimeMode.PAPER_EXECUTE:
            raise ValueError("paper_execute is not available in M1")


class StartupGate:
    """In-memory, per-process. Set at construction; cleared only by a clean startup."""

    def __init__(self) -> None:
        self._completed_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return self._completed_at is not None

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at

    def _mark_complete(self, at: datetime) -> None:
        self._completed_at = at


@dataclass(slots=True)
class _BrokerView:
    account: AccountSnapshot | None = None
    config: AccountConfigSnapshot | None = None
    clock: MarketClock | None = None
    findings: list[GateFinding] = field(default_factory=list)


class TradingGates:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        loss_limits: LossLimitMonitor,
        broker: BrokerReadPort,
        config: GateConfig,
        *,
        startup: StartupGate | None = None,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._loss = loss_limits
        self._broker = broker
        self._config = config
        self.startup = startup if startup is not None else StartupGate()

    # ----------------------------------------------------------------- public
    def evaluate(self, *, now: datetime) -> GateStatus:
        """Read-only evaluation of every gate at ``now``."""
        broker = self._read_broker()
        with self._db.read() as s:
            findings = [*broker.findings, *self._ledger_findings(s, now, broker)]
        if not self.startup.complete:
            findings.append(
                GateFinding(
                    GateLayer.STARTUP,
                    GateReason.STARTUP_INCOMPLETE,
                    "startup readiness has not passed",
                )
            )
        return GateStatus(
            evaluated_at=now,
            mode=self._config.mode,
            startup_complete=self.startup.complete,
            findings=tuple(sorted(set(findings))),
        )

    def run_startup(self, *, now: datetime) -> GateStatus:
        """Clear the startup gate if every STARTUP-layer prerequisite passes. This never
        clears, overrides or ignores a persistent halt or a transient block."""
        status = self.evaluate(now=now)
        prerequisites = [
            f
            for f in status.findings
            if f.layer is GateLayer.STARTUP and f.reason is not GateReason.STARTUP_INCOMPLETE
        ]
        if not prerequisites and not self.startup.complete:
            self.startup._mark_complete(now)
            status = self.evaluate(now=now)
        return status

    # ----------------------------------------------------------------- broker
    def _read_broker(self) -> _BrokerView:
        assert_no_transaction("broker reads for gate evaluation")
        view = _BrokerView()
        view.account = self._call(view, "account", self._broker.get_account)
        view.config = self._call(view, "account configuration", self._broker.get_account_config)
        view.clock = self._call(view, "clock", self._broker.get_clock)
        return view

    @staticmethod
    def _call[T](view: _BrokerView, what: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except AuthenticationError as exc:
            reason, detail = GateReason.BROKER_AUTH_FAILED, f"{what}: {exc.status_code}"
        except (InvalidBrokerDataError, IncompleteRetrievalError) as exc:
            reason, detail = GateReason.BROKER_DATA_INVALID, f"{what}: {exc}"
        except BrokerError as exc:
            reason, detail = GateReason.BROKER_UNAVAILABLE, f"{what}: {type(exc).__name__}"
        view.findings.append(GateFinding(GateLayer.TRANSIENT, reason, detail))
        return None

    # ----------------------------------------------------------------- ledger
    def _ledger_findings(self, s: Session, now: datetime, broker: _BrokerView) -> list[GateFinding]:
        out: list[GateFinding] = []
        out += self._startup_prerequisites(s, now, broker)
        out += self._persistent(s)
        out += self._transient(s, now, broker)
        return out

    def _startup_prerequisites(
        self, s: Session, now: datetime, broker: _BrokerView
    ) -> list[GateFinding]:
        st = GateLayer.STARTUP
        out: list[GateFinding] = []
        if self._db.identity.mode is not self._config.mode:
            out.append(
                GateFinding(
                    st,
                    GateReason.LEDGER_MODE_MISMATCH,
                    f"ledger {self._db.identity.mode.value} != {self._config.mode.value}",
                )
            )
        report = integrity_report(s)
        if not report.ok:
            out.append(
                GateFinding(st, GateReason.LEDGER_INTEGRITY_FAILED, "; ".join(report.problems()))
            )
        try:
            self._pf.portfolio(s)
        except PersistenceError:
            out.append(GateFinding(st, GateReason.PORTFOLIO_MISSING, "ledger not initialized"))
            return out  # nothing below is meaningful without a portfolio
        today = market_date(now)
        invariants = accounting_checker(lambda: today).run(s)
        if not invariants.ok:
            names = sorted({v.invariant for v in invariants.violations})
            out.append(GateFinding(st, GateReason.LEDGER_INVARIANT_VIOLATED, ", ".join(names)))
        out += self._identity(s, broker)
        if not self.startup.complete:
            latest = ReconciliationRunsRepository().latest(s)
            if latest is None:
                why = "no reconciliation run"
            elif latest.result.value != "OK":
                why = f"latest run {latest.id} is {latest.result.value}"
            elif now - latest.ts > self._config.reconciliation_max_age:
                why = f"latest clean run {latest.ts.isoformat()} is too old"
            else:
                why = ""
            if why:
                out.append(GateFinding(st, GateReason.RECONCILIATION_REQUIRED, why))
        return out

    def _identity(self, s: Session, broker: _BrokerView) -> list[GateFinding]:
        st = GateLayer.STARTUP
        out: list[GateFinding] = []
        sync = SyncStateRepository()
        recorded_id = sync.get(s, BROKER_ACCOUNT_ID)
        recorded_env = sync.get(s, BROKER_ENVIRONMENT)
        expected_env = EXPECTED_ENVIRONMENT[self._config.mode]
        if recorded_id is None or recorded_env is None:
            out.append(
                GateFinding(
                    st,
                    GateReason.BROKER_IDENTITY_UNRECORDED,
                    "broker account id/environment not recorded",
                )
            )
        if recorded_env is not None and recorded_env != expected_env:
            out.append(
                GateFinding(
                    st,
                    GateReason.BROKER_ENVIRONMENT_MISMATCH,
                    f"ledger recorded {recorded_env!r}, mode needs {expected_env!r}",
                )
            )
        account = broker.account
        if account is not None:
            if account.environment != expected_env:
                out.append(
                    GateFinding(
                        st,
                        GateReason.BROKER_ENVIRONMENT_MISMATCH,
                        f"broker is {account.environment!r}, mode needs {expected_env!r}",
                    )
                )
            if recorded_id is not None and account.account_id != recorded_id:
                out.append(
                    GateFinding(
                        st,
                        GateReason.BROKER_IDENTITY_MISMATCH,
                        "connected account differs from the recorded account",
                    )
                )
        config = broker.config
        if config is not None:
            unsupported = []
            if config.suspend_trade:
                unsupported.append("suspend_trade=true")
            if config.fractional_trading:
                unsupported.append("fractional_trading=true")
            if unsupported:
                out.append(
                    GateFinding(
                        st, GateReason.BROKER_ACCOUNT_CONFIG_UNSUPPORTED, ", ".join(unsupported)
                    )
                )
            if not config.m2_protections_in_place:
                layer = st if self._config.require_m2_account_protections else GateLayer.ADVISORY
                out.append(
                    GateFinding(
                        layer,
                        GateReason.ACCOUNT_PROTECTIONS_NOT_SET,
                        f"no_shorting={config.no_shorting}, max_margin_multiplier="
                        f"{config.max_margin_multiplier} (U6: required before M2)",
                    )
                )
        return out

    def _persistent(self, s: Session) -> list[GateFinding]:
        state = TradingStateRepository().get(s)
        if not state.halted:
            return []
        return [
            GateFinding(
                GateLayer.PERSISTENT,
                GateReason.PERSISTENT_HALT,
                f"{state.reason_code}: {state.reason_detail or ''}".strip(),
            )
        ]

    def _transient(self, s: Session, now: datetime, broker: _BrokerView) -> list[GateFinding]:
        tr = GateLayer.TRANSIENT
        c = self._config
        out: list[GateFinding] = []
        if c.mode is RuntimeMode.OBSERVE:
            out.append(
                GateFinding(
                    tr, GateReason.RUNTIME_MODE_NO_EXECUTION, "observe mode has no execution port"
                )
            )
        if not c.trading_enabled:
            out.append(GateFinding(tr, GateReason.TRADING_DISABLED, "TRADING_ENABLED=false"))
        account = broker.account
        if account is not None and (
            account.status != "ACTIVE" or account.trading_blocked or account.account_blocked
        ):
            out.append(
                GateFinding(
                    tr,
                    GateReason.BROKER_ACCOUNT_RESTRICTED,
                    f"status={account.status}, trading_blocked="
                    f"{account.trading_blocked}, account_blocked="
                    f"{account.account_blocked}",
                )
            )
        if broker.clock is not None and not broker.clock.is_open:
            out.append(
                GateFinding(
                    tr, GateReason.MARKET_CLOSED, f"next open {broker.clock.next_open.isoformat()}"
                )
            )
        out += self._freshness(s, now)
        out += self._calendar(s, now)
        out += self._orders_and_activities(s)
        try:
            snapshot = self._pf.snapshot(s, now=now)
            loss = self._loss.report(s, now=now)
        except PersistenceError as exc:  # pragma: no cover - guarded by PORTFOLIO_MISSING
            return [*out, GateFinding(tr, GateReason.UNMAPPED_BLOCKER, str(exc))]
        out += self._upstream(b.value for b in snapshot.entry_blockers)
        out += self._upstream(b.value for b in loss.blockers)
        return out

    def _freshness(self, s: Session, now: datetime) -> list[GateFinding]:
        tr = GateLayer.TRANSIENT
        c = self._config
        out: list[GateFinding] = []
        streak = load_streak(s)
        if streak is not None:
            # A failed sync cycle blocks new entries at once, before any escalation and
            # regardless of how recent the last successful sync was (§12.21).
            out.append(
                GateFinding(
                    tr,
                    GateReason.BROKER_OUTAGE_ACTIVE,
                    f"{streak.counted_cycles} failed sync cycle(s) since "
                    f"{streak.first_failed_at.isoformat()} ({streak.category})",
                )
            )
        last_sync = SyncStateRepository().get(s, LAST_SUCCESSFUL_SYNC)
        if last_sync is None or now - ts_from_db(last_sync) > c.max_sync_age:
            out.append(
                GateFinding(
                    tr,
                    GateReason.BROKER_SYNC_STALE,
                    "never synced" if last_sync is None else f"last {last_sync}",
                )
            )
        covered = complete_through(s)
        limit = now - c.activity_posting_lag - c.max_sync_age
        if covered is None or covered < limit:
            out.append(
                GateFinding(
                    tr,
                    GateReason.ACTIVITY_COVERAGE_INCOMPLETE,
                    "no complete activity window"
                    if covered is None
                    else f"complete through {covered.isoformat()}",
                )
            )
        latest = ReconciliationRunsRepository().latest(s)
        if latest is not None:
            if latest.result.value == "INCONCLUSIVE":
                out.append(
                    GateFinding(tr, GateReason.RECONCILIATION_INCONCLUSIVE, f"run {latest.id}")
                )
            elif latest.result.value in {"MISMATCH", "ERROR"}:
                out.append(
                    GateFinding(
                        tr,
                        GateReason.RECONCILIATION_MISMATCH,
                        f"run {latest.id}: {latest.result.value}",
                    )
                )
            elif now - latest.ts > c.reconciliation_max_age:
                out.append(
                    GateFinding(
                        tr,
                        GateReason.RECONCILIATION_STALE,
                        f"last clean run {latest.ts.isoformat()}",
                    )
                )
        return out

    def _calendar(self, s: Session, now: datetime) -> list[GateFinding]:
        tr = GateLayer.TRANSIENT
        c = self._config
        try:
            calendar = CalendarStore().calendar(s, as_of=now, max_age=c.calendar_max_age)
        except CalendarError as exc:
            return [GateFinding(tr, GateReason.CALENDAR_UNAVAILABLE, str(exc)[:200])]
        out: list[GateFinding] = []
        try:
            calendar.require_future_coverage(c.calendar_coverage_days)
        except CalendarError as exc:
            out.append(GateFinding(tr, GateReason.CALENDAR_COVERAGE_INSUFFICIENT, str(exc)[:200]))
        horizon = calendar.horizon_status(c.calendar_horizon_warning_days)
        if horizon.warning:
            out.append(
                GateFinding(
                    GateLayer.ADVISORY,
                    GateReason.CALENDAR_HORIZON_SHORT,
                    f"{horizon.days_remaining} days of fresh coverage",
                )
            )
        return out

    @staticmethod
    def _orders_and_activities(s: Session) -> list[GateFinding]:
        tr = GateLayer.TRANSIENT
        out: list[GateFinding] = []
        marks = ", ".join("?" * len(UNRESOLVED_ORDER_STATUSES))
        rows = s.fetch_all(
            f"SELECT client_order_id, status FROM orders WHERE status IN ({marks}) "  # noqa: S608
            "ORDER BY client_order_id",
            [st.value for st in UNRESOLVED_ORDER_STATUSES],
        )
        if rows:
            out.append(
                GateFinding(
                    tr, GateReason.UNRESOLVED_ORDERS, "; ".join(f"{r[0]}={r[1]}" for r in rows)
                )
            )
        unbooked = s.fetch_all(
            "SELECT client_order_id, broker_filled_qty, filled_qty FROM orders "
            "WHERE broker_filled_qty IS NOT NULL ORDER BY client_order_id"
        )
        pending = [r for r in unbooked if Decimal(str(r[1])) > Decimal(str(r[2]))]
        if pending:
            out.append(
                GateFinding(
                    tr,
                    GateReason.UNBOOKED_FILLS,
                    "; ".join(f"{r[0]}: broker {r[1]} booked {r[2]}" for r in pending),
                )
            )
        # Unexplained activities block entries until resolved. Only events acknowledged
        # one by one by an approved rebaseline (unchanged payload) stop blocking; a newly
        # discovered event blocks whatever its timestamp (§12.19).
        unexplained = s.fetch_all(
            "SELECT activity_id FROM broker_activities WHERE attribution = ? "  # noqa: S608
            f"AND NOT {ACKNOWLEDGED_ACTIVITY_SQL} ORDER BY activity_id",
            [ActivityAttribution.UNEXPLAINED.value],
        )
        if unexplained:
            out.append(
                GateFinding(
                    tr, GateReason.UNEXPLAINED_ACTIVITY, ", ".join(str(r[0]) for r in unexplained)
                )
            )
        return out

    @staticmethod
    def _upstream(codes: Iterable[str]) -> list[GateFinding]:
        out = []
        for code in codes:
            reason = _UPSTREAM.get(code)
            if reason is None:
                out.append(GateFinding(GateLayer.TRANSIENT, GateReason.UNMAPPED_BLOCKER, code))
            else:
                out.append(GateFinding(GateLayer.TRANSIENT, reason))
        return out
