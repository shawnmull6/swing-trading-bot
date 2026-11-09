"""Equity baselines and loss limits (ARCHITECTURE §11.2-11.3, U1-U3, M1 step 7).

Sessions
--------
* **Current session**: today's session while it is open (or before its open); after
  today's close or on a non-trading day, the next trading session.
* **Daily baseline** for the current session: equity at the **official close** of the
  previous trading session (cash journal up to that close + quantities held at that
  close x each symbol's recorded official close). If the portfolio did not yet exist at
  that close, the baseline is its inception capital.
* **Weekly baseline** for the calendar week (Mon-Sun, America/New_York) containing the
  current session: equity at the close of the last trading session before that week's
  Monday. Both baselines are written once and never recomputed or reset — not by a
  restart and not by an operator resume.

Limits
------
* Daily: loss >= 2% of the daily baseline -> ``DAILY_LOCKOUT`` for the current session
  (blocks new entries). It expires only once a baseline for a later session exists.
* Repeat: a lockout when another lockout already exists within the 5-session window
  (this session + the 4 before) -> ``DAILY_REPEAT_HALT`` + persistent halt.
* Weekly: drawdown >= 5% of the weekly baseline -> ``WEEKLY_HALT`` + persistent halt; the
  week's breach keeps blocking new entries for the rest of the week even after a resume.
* Equity is mark-to-market with fresh marks only, so overnight gaps count toward both
  limits at the first evaluation that has a valid price. Missing/stale marks, calendar
  data or baselines block new entries (and no breach can be evaluated).

Blocks here apply to **new entries only**. Risk-reducing exits remain conceptually
permissible during lockouts and halts (exit execution is not implemented in M1).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from swing_trading_bot.config.risk_limits import RiskLimits
from swing_trading_bot.domain.calendar import (
    MARKET_TZ,
    CalendarError,
    TradingCalendar,
    TradingSession,
)
from swing_trading_bot.domain.enums import HaltReason, Side
from swing_trading_bot.domain.loss_limits import (
    GapObservation,
    GapStressReport,
    LimitMeasure,
    gap_stress,
)
from swing_trading_bot.domain.money import ZERO, to_decimal
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.codec import dec_from_db, str_from_db, ts_to_db
from swing_trading_bot.ledger.db import Database, Session, Tx
from swing_trading_bot.ledger.loss_limit_store import (
    BaselineBasis,
    EquitySnapshot,
    EquitySnapshotsRepository,
    LossEvent,
    LossEventKind,
    LossLimitEventsRepository,
    SessionClose,
    SessionClosesRepository,
    SnapshotKind,
)
from swing_trading_bot.ledger.portfolio import PortfolioSnapshot, VirtualPortfolio
from swing_trading_bot.ledger.repositories import IngestResult, TradingStateRepository

LOSS_LIMIT_ACTOR = "loss_limits"


@dataclass(frozen=True, slots=True)
class LossLimitConfig:
    daily_pct: Decimal
    weekly_pct: Decimal
    escalation_count: int
    escalation_sessions: int
    gap_scenario_pct: Decimal
    gap_position_pct: Decimal
    gap_portfolio_pct: Decimal
    calendar_max_age: timedelta

    @classmethod
    def from_risk_limits(cls, r: RiskLimits, *, calendar_max_age: timedelta) -> LossLimitConfig:
        return cls(
            daily_pct=r.loss_limits.max_daily_loss_pct,
            weekly_pct=r.loss_limits.max_weekly_drawdown_pct,
            escalation_count=r.loss_limits.daily_lockout_escalation_count,
            escalation_sessions=r.loss_limits.daily_lockout_escalation_sessions,
            gap_scenario_pct=r.gap_stress.scenario_gap_pct,
            gap_position_pct=r.gap_stress.max_position_scenario_loss_pct_equity,
            gap_portfolio_pct=r.gap_stress.max_portfolio_scenario_loss_pct_equity,
            calendar_max_age=calendar_max_age,
        )


class LossBlocker(StrEnum):
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    SESSION_BASELINE_MISSING = "SESSION_BASELINE_MISSING"
    WEEK_BASELINE_MISSING = "WEEK_BASELINE_MISSING"
    EQUITY_UNKNOWN = "EQUITY_UNKNOWN"
    DAILY_LOSS_LOCKOUT = "DAILY_LOSS_LOCKOUT"
    WEEKLY_LIMIT_ACTIVE = "WEEKLY_LIMIT_ACTIVE"
    GAP_STRESS_PORTFOLIO_EXCEEDED = "GAP_STRESS_PORTFOLIO_EXCEEDED"


@dataclass(frozen=True, slots=True)
class SessionContext:
    current: TradingSession
    reference: TradingSession
    week_start: date
    week_reference: TradingSession
    window: tuple[date, ...] | None
    """Current session plus the previous (N-1) sessions; None if not determinable."""


@dataclass(frozen=True, slots=True)
class LossLimitReport:
    as_of: datetime
    current_session: date | None
    week_start: date | None
    daily_baseline: EquitySnapshot | None
    weekly_baseline: EquitySnapshot | None
    equity: Decimal | None
    daily: LimitMeasure | None
    weekly: LimitMeasure | None
    lockout_active: bool
    lockout_session: date | None
    weekly_breach_active: bool
    recent_lockouts: tuple[date, ...]
    window: tuple[date, ...] | None
    gaps: tuple[GapObservation, ...]
    gap_stress: GapStressReport | None
    blockers: tuple[LossBlocker, ...]
    problems: tuple[str, ...] = field(default_factory=tuple)

    RISK_REDUCING_EXITS_PERMITTED: ClassVar[bool] = True
    """Loss-limit blocks and halts stop new entries only (exits are not built in M1)."""

    @property
    def blocks_new_entries(self) -> bool:
        return bool(self.blockers)


def current_session_for(calendar: TradingCalendar, now: datetime) -> TradingSession:
    today = now.astimezone(MARKET_TZ).date()
    if calendar.is_trading_session(today):
        session = calendar.session(today)
        if now < session.close_at:
            return session
    return calendar.session(calendar.next_trading_session(today))


def session_context(calendar: TradingCalendar, now: datetime, window_size: int) -> SessionContext:
    current = current_session_for(calendar, now)
    reference = calendar.session(calendar.previous_trading_session(current.session_date))
    week_start = current.session_date - timedelta(days=current.session_date.weekday())
    week_reference = calendar.session(calendar.previous_trading_session(week_start))
    window: tuple[date, ...] | None
    try:
        days = [current.session_date]
        for _ in range(window_size - 1):
            days.append(calendar.previous_trading_session(days[-1]))
        window = tuple(days)
    except CalendarError:
        window = None
    return SessionContext(current, reference, week_start, week_reference, window)


class LossLimitMonitor:
    def __init__(self, db: Database, portfolio: VirtualPortfolio, config: LossLimitConfig) -> None:
        self._db = db
        self._pf = portfolio
        self._config = config
        self._closes = SessionClosesRepository()
        self._snapshots = EquitySnapshotsRepository()
        self._events = LossLimitEventsRepository()
        self._state = TradingStateRepository()
        self._calendars = CalendarStore()

    # ------------------------------------------------------------- inputs
    def record_session_close(self, close: SessionClose) -> IngestResult:
        """Store a symbol's official close for a session (immutable). A conflicting
        re-record is reported, never applied."""
        with self._db.transaction() as tx:
            return self._closes.record(tx, close)

    # ----------------------------------------------------------- baselines
    def _calendar(self, s: Session, now: datetime) -> TradingCalendar:
        return self._calendars.calendar(s, as_of=now, max_age=self._config.calendar_max_age)

    def _equity_at_close(
        self,
        s: Session,
        kind: SnapshotKind,
        *,
        applies_to: date,
        ref: TradingSession,
        now: datetime,
    ) -> tuple[EquitySnapshot | None, list[str]]:
        portfolio = self._pf.portfolio(s)
        pid = portfolio.id
        if pid is None:  # pragma: no cover - stored rows always have ids
            raise RuntimeError("portfolio without id")
        if portfolio.created_at > ref.close_at:
            initial = portfolio.initial_capital
            return (
                EquitySnapshot(pid, kind, applies_to, None, portfolio.created_at,
                               BaselineBasis.INCEPTION, initial, initial, ZERO, [], now),
                [],
            )  # fmt: skip
        close_ts = ts_to_db(ref.close_at)
        cash = sum(
            (dec_from_db(r[0]) for r in s.fetch_all(
                "SELECT amount FROM cash_entries WHERE portfolio_id = ? AND ts <= ?",
                (pid, close_ts))),
            ZERO,
        )  # fmt: skip
        qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for sym, side, q in s.fetch_all(
            "SELECT f.symbol, f.side, f.qty FROM fills f JOIN orders o "
            "ON o.client_order_id = f.client_order_id "
            "WHERE o.portfolio_id = ? AND f.filled_at <= ?",
            (pid, close_ts),
        ):
            amount = dec_from_db(q)
            qty[str_from_db(sym)] += amount if Side(str_from_db(side)) is Side.BUY else -amount
        marks: list[dict[str, Any]] = []
        missing: list[str] = []
        market_value = ZERO
        for sym in sorted(qty):
            if qty[sym] == ZERO:
                continue
            close = self._closes.get(s, sym, ref.session_date)
            if close is None:
                missing.append(f"no official close for {sym} on {ref.session_date}")
                continue
            market_value += qty[sym] * close.close_price
            marks.append(
                {"symbol": sym, "qty": qty[sym], "close": close.close_price,
                 "source": close.source.value, "as_of": close.as_of.isoformat()}
            )  # fmt: skip
        if missing:
            return None, missing
        return (
            EquitySnapshot(pid, kind, applies_to, ref.session_date, ref.close_at,
                           BaselineBasis.SESSION_CLOSE, cash + market_value, cash,
                           market_value, marks, now),
            [],
        )  # fmt: skip

    def establish_baselines(
        self, *, now: datetime
    ) -> tuple[EquitySnapshot | None, EquitySnapshot | None, tuple[str, ...]]:
        """Write the current session and week baselines if missing (idempotent)."""
        with self._db.transaction() as tx:
            try:
                ctx = session_context(
                    self._calendar(tx, now), now, self._config.escalation_sessions
                )
            except CalendarError as exc:
                return None, None, (f"calendar: {exc}",)
            problems: list[str] = []
            daily = self._ensure(
                tx, SnapshotKind.SESSION_BASELINE, applies_to=ctx.current.session_date,
                ref=ctx.reference, now=now, problems=problems,
            )  # fmt: skip
            weekly = self._ensure(
                tx, SnapshotKind.WEEK_BASELINE, applies_to=ctx.week_start,
                ref=ctx.week_reference, now=now, problems=problems,
            )  # fmt: skip
        return daily, weekly, tuple(problems)

    def _ensure(
        self,
        tx: Tx,
        kind: SnapshotKind,
        *,
        applies_to: date,
        ref: TradingSession,
        now: datetime,
        problems: list[str],
    ) -> EquitySnapshot | None:
        pid = self._pf.portfolio(tx).id
        if pid is None:  # pragma: no cover
            raise RuntimeError("portfolio without id")
        existing = self._snapshots.get(tx, pid, kind, applies_to)
        if existing is not None:
            return existing
        candidate, missing = self._equity_at_close(
            tx, kind, applies_to=applies_to, ref=ref, now=now
        )
        problems.extend(missing)
        if candidate is None:
            return None
        if candidate.equity <= ZERO:
            problems.append(f"{kind.value} {applies_to}: non-positive baseline equity")
            return None
        return self._snapshots.insert_once(tx, candidate)

    # ---------------------------------------------------------- evaluation
    def evaluate(self, *, now: datetime) -> LossLimitReport:
        """Establish baselines, measure mark-to-market equity, record breaches (once) and
        set persistent halts where required. Returns the resulting report."""
        _, _, baseline_problems = self.establish_baselines(now=now)
        with self._db.read() as s:
            before = self._build(s, now, baseline_problems)
        self._record_breaches(before, now)
        with self._db.read() as s:
            return self._build(s, now, baseline_problems)

    def report(self, s: Session, *, now: datetime) -> LossLimitReport:
        """Read-only status: never writes baselines, events or halts."""
        return self._build(s, now, ())

    def _record_breaches(self, r: LossLimitReport, now: datetime) -> None:
        if r.equity is None or r.current_session is None:
            return
        with self._db.transaction() as tx:
            pid = self._pf.portfolio(tx).id
            if pid is None:  # pragma: no cover
                raise RuntimeError("portfolio without id")
            evidence = {"equity": r.equity, "gaps": [
                {"symbol": g.symbol, "qty": g.qty_at_close, "prior_close": g.prior_close,
                 "price": g.price, "pnl": g.pnl} for g in r.gaps]}  # fmt: skip
            if r.daily is not None and r.daily_baseline is not None and r.daily.breached:
                inserted, _ = self._events.insert_once(
                    tx, self._event(pid, LossEventKind.DAILY_LOCKOUT, applies_to=r.current_session,
                    baseline=r.daily_baseline, measure=r.daily, now=now,
                    detail=evidence),
                )  # fmt: skip
                if inserted:
                    self._check_repeat(tx, pid, r, now, evidence)
            if (
                r.weekly is not None
                and r.weekly_baseline is not None
                and r.week_start is not None
                and r.weekly.breached
            ):
                inserted, _ = self._events.insert_once(
                    tx, self._event(pid, LossEventKind.WEEKLY_HALT, applies_to=r.week_start,
                    baseline=r.weekly_baseline, measure=r.weekly, now=now,
                    detail=evidence),
                )  # fmt: skip
                if inserted:
                    self._state.halt(
                        tx, HaltReason.WEEKLY_DRAWDOWN,
                        detail=f"weekly drawdown {r.weekly.loss_pct} >= "
                               f"{self._config.weekly_pct} of {r.weekly.baseline}",
                        actor=LOSS_LIMIT_ACTOR, at=now,
                    )  # fmt: skip

    def _check_repeat(
        self, tx: Tx, pid: int, r: LossLimitReport, now: datetime, evidence: dict[str, Any]
    ) -> None:
        if r.current_session is None or r.daily is None or r.daily_baseline is None:
            return  # pragma: no cover - guarded by the caller
        lockouts = {e.applies_to for e in self._events.list(tx, pid, LossEventKind.DAILY_LOCKOUT)}
        if r.window is None:
            repeat, why = True, "lockout window not determinable from calendar (fail closed)"
        else:
            in_window = sorted(lockouts & set(r.window))
            repeat = len(in_window) >= self._config.escalation_count
            why = f"lockouts in window {[d.isoformat() for d in in_window]}"
        if not repeat:
            return
        inserted, _ = self._events.insert_once(
            tx, self._event(pid, LossEventKind.DAILY_REPEAT_HALT, applies_to=r.current_session,
                    baseline=r.daily_baseline, measure=r.daily, now=now,
                    detail={**evidence, "reason": why}),
        )  # fmt: skip
        if inserted:
            self._state.halt(tx, HaltReason.DAILY_LOSS_REPEAT, detail=why,
                             actor=LOSS_LIMIT_ACTOR, at=now)  # fmt: skip

    def _event(
        self,
        pid: int,
        kind: LossEventKind,
        *,
        applies_to: date,
        baseline: EquitySnapshot,
        measure: LimitMeasure,
        now: datetime,
        detail: dict[str, Any],
    ) -> LossEvent:
        if baseline.id is None:  # pragma: no cover
            raise RuntimeError("baseline without id")
        return LossEvent(
            portfolio_id=pid,
            ts=now,
            kind=kind,
            applies_to=applies_to,
            baseline_snapshot_id=baseline.id,
            baseline_equity=measure.baseline,
            equity=measure.equity,
            loss=measure.loss,
            loss_pct=measure.loss_pct,
            threshold_pct=measure.threshold,
            detail=detail,
        )

    # ------------------------------------------------------------- report
    def _build(self, s: Session, now: datetime, extra_problems: tuple[str, ...]) -> LossLimitReport:
        snap = self._pf.snapshot(s, now=now)
        pid = snap.balances.portfolio_id
        blockers: list[LossBlocker] = []
        problems = list(extra_problems)
        try:
            ctx: SessionContext | None = session_context(
                self._calendar(s, now), now, self._config.escalation_sessions
            )
        except CalendarError as exc:
            ctx = None
            problems.append(f"calendar: {exc}")
            blockers.append(LossBlocker.CALENDAR_UNAVAILABLE)
        if snap.equity is None:
            blockers.append(LossBlocker.EQUITY_UNKNOWN)
        if ctx is None:
            return LossLimitReport(
                as_of=now, current_session=None, week_start=None, daily_baseline=None,
                weekly_baseline=None, equity=snap.equity, daily=None, weekly=None,
                lockout_active=False, lockout_session=None, weekly_breach_active=False,
                recent_lockouts=(), window=None, gaps=(), gap_stress=None,
                blockers=tuple(blockers),
                problems=tuple(problems),
            )  # fmt: skip
        current = ctx.current.session_date
        daily_base = self._snapshots.get(s, pid, SnapshotKind.SESSION_BASELINE, current)
        weekly_base = self._snapshots.get(s, pid, SnapshotKind.WEEK_BASELINE, ctx.week_start)
        if daily_base is None:
            blockers.append(LossBlocker.SESSION_BASELINE_MISSING)
        if weekly_base is None:
            blockers.append(LossBlocker.WEEK_BASELINE_MISSING)
        daily = weekly = None
        if snap.equity is not None and daily_base is not None:
            daily = LimitMeasure(daily_base.equity, snap.equity, self._config.daily_pct)
        if snap.equity is not None and weekly_base is not None:
            weekly = LimitMeasure(weekly_base.equity, snap.equity, self._config.weekly_pct)

        lockouts = self._events.list(s, pid, LossEventKind.DAILY_LOCKOUT)
        lockout_session = lockouts[-1].applies_to if lockouts else None
        lockout_active = lockout_session is not None and (
            lockout_session == current
            or not self._snapshots.exists_after(
                s, pid, SnapshotKind.SESSION_BASELINE, lockout_session
            )
        )
        if lockout_active:
            blockers.append(LossBlocker.DAILY_LOSS_LOCKOUT)
        weekly_active = (
            self._events.get(s, pid, LossEventKind.WEEKLY_HALT, ctx.week_start) is not None
        )
        if weekly_active:
            blockers.append(LossBlocker.WEEKLY_LIMIT_ACTIVE)
        window = ctx.window
        recent = tuple(sorted(e.applies_to for e in lockouts if window and e.applies_to in window))

        gaps = self._gaps(daily_base, snap)
        stress = self._stress(daily_base, snap)
        if stress is not None and stress.portfolio_exceeded:
            blockers.append(LossBlocker.GAP_STRESS_PORTFOLIO_EXCEEDED)
        return LossLimitReport(
            as_of=now, current_session=current, week_start=ctx.week_start,
            daily_baseline=daily_base, weekly_baseline=weekly_base, equity=snap.equity,
            daily=daily, weekly=weekly, lockout_active=lockout_active,
            lockout_session=lockout_session, weekly_breach_active=weekly_active,
            recent_lockouts=recent, window=window, gaps=gaps, gap_stress=stress,
            blockers=tuple(dict.fromkeys(blockers)), problems=tuple(problems),
        )  # fmt: skip

    @staticmethod
    def _gaps(
        baseline: EquitySnapshot | None, snap: PortfolioSnapshot
    ) -> tuple[GapObservation, ...]:
        if baseline is None or baseline.basis is not BaselineBasis.SESSION_CLOSE:
            return ()
        prices = {
            p.symbol: p.mark.price
            for p in snap.positions
            if p.mark is not None and p.mark_assessment.fresh
        }
        out = []
        for m in baseline.marks:
            sym = str(m["symbol"])
            if sym in prices:
                out.append(
                    GapObservation(sym, to_decimal(m["qty"]), to_decimal(m["close"]),
                                   prices[sym])
                )  # fmt: skip
        return tuple(out)

    def _stress(
        self, baseline: EquitySnapshot | None, snap: PortfolioSnapshot
    ) -> GapStressReport | None:
        if baseline is None or baseline.equity <= ZERO or snap.market_value is None:
            return None
        values = {
            p.symbol: p.market_value
            for p in snap.positions
            if p.market_value is not None and p.qty != ZERO
        }
        return gap_stress(
            values,
            snap.balances.buy_commitments,
            denominator=baseline.equity,
            scenario_pct=self._config.gap_scenario_pct,
            position_limit_pct=self._config.gap_position_pct,
            portfolio_limit_pct=self._config.gap_portfolio_pct,
        )
