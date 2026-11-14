"""Consolidated SIP reference closes for loss-limit baselines (M2-2b, Step 16.9,
ARCHITECTURE §12.37).

A reference close is the close of the **raw** consolidated (SIP) daily bar of a completed
session, recorded once as ``CloseSource.CONSOLIDATED_BAR``. It is not an official close:
Step 16.8 showed Alpaca's SIP daily open/high/low/close exclude extended-hours trades and
the close is a 16:00 ET print, but equivalence to the exchange's official auction close
is unverified. Nothing here runs unless ``LossLimitConfig.consolidated_closes_enabled``
is set (default off; the real OBSERVE runtime keeps it off).

Acquisition (one call per sync cycle, no scheduler):

* reference sessions are the current daily and weekly baselines' reference sessions;
* only symbols the ledger held at that session's close; none held → no request; all
  already recorded → no request;
* eligible only at or after **20:00 America/New_York on the session date** (normal and
  early-close sessions alike; weekends and holidays never are reference sessions, so the
  first cycle after the threshold — that evening or the next trading morning — records);
* one ``get_daily_bars(symbols, d, d, feed="sip", adjustment="raw")``; a broker error
  records nothing (the baseline stays missing and entries stay blocked);
* no IEX fallback, no replacement, interpolation or adjustment of any price.

Admission per symbol (any failure: not recorded, reason reported):

* exactly one bar, for that session, from ``sip`` / ``raw``;
* volume > 0;
* VWAP present and ``abs(close - vwap) <= max_vwap_deviation * vwap`` (default 10%). Alpaca's
  daily VWAP and volume include extended-hours trades (Step 16.8), so this is only a
  coarse guard against an anomalous closing print: passing it does **not** show that the
  close is official or accurate;
* the bar was received at or after the 20:00 threshold.

Recording is append-only: an identical later observation is a duplicate; a different one
is a conflict, kept in ``session_close_conflicts`` (the recorded close never changes),
which blocks new entries while it affects a current baseline (``LossBlocker
.REFERENCE_CLOSE_CONFLICT``). Nothing clears a conflict automatically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.broker.errors import BrokerError
from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.broker.ports import BarSource
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarError, TradingSession
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database, assert_no_transaction
from swing_trading_bot.ledger.loss_limit_store import (
    CONSOLIDATED_ADJUSTMENT,
    CONSOLIDATED_FEED,
    CloseConflict,
    CloseSource,
    SessionClose,
    SessionCloseConflictsRepository,
    SessionClosesRepository,
)
from swing_trading_bot.ledger.loss_limits import (
    LossLimitConfig,
    before_pid,
    quantities_at_close,
    session_context,
)
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import IngestResult

RECORDING_TIME: Final = time(20, 0)
DEFAULT_MAX_VWAP_DEVIATION: Final = Decimal("0.10")


def recording_eligible_at(session_date: date) -> datetime:
    """20:00 America/New_York on the session date (also for early-close sessions)."""
    return datetime.combine(session_date, RECORDING_TIME, tzinfo=MARKET_TZ)


class Rejection(StrEnum):
    MISSING_BAR = "MISSING_BAR"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    WRONG_SESSION = "WRONG_SESSION"
    WRONG_FEED = "WRONG_FEED"
    WRONG_ADJUSTMENT = "WRONG_ADJUSTMENT"
    ZERO_VOLUME = "ZERO_VOLUME"
    VWAP_MISSING = "VWAP_MISSING"
    CLOSE_VWAP_DEVIATION = "CLOSE_VWAP_DEVIATION"
    OBSERVED_TOO_EARLY = "OBSERVED_TOO_EARLY"


def admit(
    symbol: str,
    bars: Sequence[DailyBar],
    *,
    session_date: date,
    max_vwap_deviation: Decimal = DEFAULT_MAX_VWAP_DEVIATION,
) -> SessionClose | Rejection:
    """A ``CONSOLIDATED_BAR`` close from ``symbol``'s bars for ``session_date``, or why not.
    OHLC consistency and exact numbers are already enforced by ``DailyBar``."""
    if not bars:
        return Rejection.MISSING_BAR
    bar = bars[0]
    vwap = bar.vwap
    checks = (
        (len(bars) > 1, Rejection.DUPLICATE_BAR),
        (bar.symbol != symbol or bar.session_date != session_date, Rejection.WRONG_SESSION),
        (bar.feed != CONSOLIDATED_FEED, Rejection.WRONG_FEED),
        (bar.adjustment != CONSOLIDATED_ADJUSTMENT, Rejection.WRONG_ADJUSTMENT),
        (bar.volume == 0, Rejection.ZERO_VOLUME),
        (vwap is None, Rejection.VWAP_MISSING),
        (vwap is not None and abs(bar.close - vwap) > max_vwap_deviation * vwap,
         Rejection.CLOSE_VWAP_DEVIATION),
        (bar.received_at < recording_eligible_at(session_date), Rejection.OBSERVED_TOO_EARLY),
    )  # fmt: skip
    failed = next((reason for bad, reason in checks if bad), None)
    if failed is not None:
        return failed
    return SessionClose(
        symbol=symbol,
        session_date=session_date,
        close_price=bar.close,
        source=CloseSource.CONSOLIDATED_BAR,
        as_of=bar.as_of,
        received_at=bar.received_at,
        feed=CONSOLIDATED_FEED,
        adjustment=CONSOLIDATED_ADJUSTMENT,
    )


class OutcomeStatus(StrEnum):
    NOT_NEEDED = "NOT_NEEDED"
    """No symbol held at that close, the portfolio did not exist yet, or all recorded."""
    NOT_YET_ELIGIBLE = "NOT_YET_ELIGIBLE"
    FETCHED = "FETCHED"
    FETCH_FAILED = "FETCH_FAILED"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ReferenceCloseOutcome:
    session_date: date | None
    status: OutcomeStatus
    symbols: tuple[str, ...] = ()
    recorded: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    rejected: Mapping[str, Rejection] = field(default_factory=dict)
    error: str | None = None


class ReferenceCloseRecorder:
    """Records raw SIP reference closes for the current baselines' reference sessions."""

    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        bars: BarSource,
        config: LossLimitConfig,
        *,
        max_vwap_deviation: Decimal = DEFAULT_MAX_VWAP_DEVIATION,
    ) -> None:
        if max_vwap_deviation <= ZERO:
            raise ValueError("max_vwap_deviation must be positive")
        self._db = db
        self._pf = portfolio
        self._bars = bars
        self._config = config
        self._max_dev = max_vwap_deviation
        self._closes = SessionClosesRepository()
        self._conflicts = SessionCloseConflictsRepository()

    @property
    def enabled(self) -> bool:
        return self._config.consolidated_closes_enabled

    def run(self, *, now: datetime) -> tuple[ReferenceCloseOutcome, ...]:
        if not self.enabled:
            return ()
        with self._db.read() as s:
            try:
                calendar = CalendarStore().calendar(
                    s, as_of=now, max_age=self._config.calendar_max_age
                )
                ctx = session_context(calendar, now, self._config.escalation_sessions)
            except CalendarError as exc:
                return (ReferenceCloseOutcome(None, OutcomeStatus.CALENDAR_UNAVAILABLE,
                                              error=str(exc)),)  # fmt: skip
            pid = before_pid(self._pf, s)
            created_at = self._pf.portfolio(s).created_at
            refs = {r.session_date: r for r in (ctx.reference, ctx.week_reference)}
            plan: list[tuple[TradingSession, tuple[str, ...], bool]] = []
            for d in sorted(refs):
                ref = refs[d]
                held = () if created_at > ref.close_at else tuple(
                    quantities_at_close(s, pid, ref.close_at))  # fmt: skip
                missing = any(
                    self._closes.get(s, sym, d, CloseSource.CONSOLIDATED_BAR) is None
                    for sym in held
                )
                plan.append((ref, held, missing))
        return tuple(
            self._acquire(ref, held, missing=missing, now=now) for ref, held, missing in plan
        )

    def _acquire(
        self, ref: TradingSession, held: tuple[str, ...], *, missing: bool, now: datetime
    ) -> ReferenceCloseOutcome:
        d = ref.session_date
        if not held or not missing:
            return ReferenceCloseOutcome(d, OutcomeStatus.NOT_NEEDED, held)
        if now < recording_eligible_at(d):
            return ReferenceCloseOutcome(d, OutcomeStatus.NOT_YET_ELIGIBLE, held)
        assert_no_transaction("reference-close bars read")
        try:  # a broker GET: never inside a ledger transaction
            bars = self._bars.get_daily_bars(held, d, d, feed="sip", adjustment="raw")
        except BrokerError as exc:
            return ReferenceCloseOutcome(d, OutcomeStatus.FETCH_FAILED, held,
                                         error=f"{type(exc).__name__}: {exc}"[:300])  # fmt: skip
        recorded: list[str] = []
        duplicates: list[str] = []
        conflicts: list[str] = []
        rejected: dict[str, Rejection] = {}
        with self._db.transaction() as tx:
            for sym in held:
                verdict = admit(sym, bars.get(sym, ()), session_date=d,
                                max_vwap_deviation=self._max_dev)  # fmt: skip
                if isinstance(verdict, Rejection):
                    rejected[sym] = verdict
                    continue
                result = self._closes.record(tx, verdict)
                if result is IngestResult.INSERTED:
                    recorded.append(sym)
                elif result is IngestResult.DUPLICATE:
                    duplicates.append(sym)
                else:
                    existing = self._closes.get(tx, sym, d, CloseSource.CONSOLIDATED_BAR)
                    assert existing is not None  # noqa: S101 - CONFLICT implies a stored row
                    self._conflicts.record(tx, CloseConflict(
                        symbol=sym, session_date=d, source=CloseSource.CONSOLIDATED_BAR,
                        recorded_close=existing.close_price, observed_close=verdict.close_price,
                        observed_as_of=verdict.as_of, observed_received_at=verdict.received_at,
                        detected_at=now,
                    ))  # fmt: skip
                    conflicts.append(sym)
        return ReferenceCloseOutcome(d, OutcomeStatus.FETCHED, held, tuple(recorded),
                                     tuple(duplicates), tuple(conflicts), rejected)  # fmt: skip


__all__ = [
    "DEFAULT_MAX_VWAP_DEVIATION",
    "RECORDING_TIME",
    "OutcomeStatus",
    "ReferenceCloseOutcome",
    "ReferenceCloseRecorder",
    "Rejection",
    "admit",
    "recording_eligible_at",
]
