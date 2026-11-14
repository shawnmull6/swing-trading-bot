"""Calendar-aware validation of historical daily bars (M2-2a, ARCHITECTURE §12.35).

Pure functions: no I/O, no indicators. Given the bars a :class:`BarSource` returned for
one symbol, decide whether that symbol has the completed-session history a caller
(future strategy code) requires, and say exactly why not otherwise.

Outcomes, in precedence order:

* ``CALENDAR_UNAVAILABLE`` — the trading calendar cannot answer for the window (stale,
  missing, contradictory). Nothing about the bars can be judged.
* ``MALFORMED`` — the broker data contradicts itself or the calendar: wrong symbol,
  feed or adjustment, out-of-order or duplicate sessions, a bar on a day that is not a
  trading session, or a source timestamp that is not the session start (midnight
  America/New_York). This is a data fault, not a history limit.
* ``INSUFFICIENT_HISTORY`` — the data is consistent but does not cover the required
  window: no bars at all, a history that starts inside the window (recent listing), a
  gap (trading suspension or missing data) or a missing most-recent session. The symbol
  is ineligible; no bar is invented and no gap is filled.
* ``INELIGIBLE`` — valid, complete data that the requirement's policy excludes. Today
  only one rule: with ``require_volume`` (the default, for the initial liquid-equity
  strategy) a zero-volume session bar makes the symbol ineligible. A zero-volume bar is
  a valid broker payload, not a data fault.
* ``ELIGIBLE`` — exactly one bar for each of the last ``lookback_sessions`` completed
  sessions.

Incomplete data is excluded before judging: a bar for a session that has not closed
yet (the current session, or any later date) and a bar observed before its session's
close (``received_at < close_at``, so early-close days use their own 13:00 close) are
never part of the history; they are reported as excluded, not as faults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from swing_trading_bot.broker.models import BarAdjustment, BarFeed, DailyBar
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarError, TradingCalendar


class HistoryStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    INELIGIBLE = "INELIGIBLE"
    MALFORMED = "MALFORMED"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"


class HistoryIssue(StrEnum):
    # data faults (MALFORMED)
    WRONG_SYMBOL = "WRONG_SYMBOL"
    WRONG_FEED = "WRONG_FEED"
    WRONG_ADJUSTMENT = "WRONG_ADJUSTMENT"
    NOT_CHRONOLOGICAL = "NOT_CHRONOLOGICAL"
    DUPLICATE_SESSION = "DUPLICATE_SESSION"
    NOT_A_TRADING_SESSION = "NOT_A_TRADING_SESSION"
    TIMESTAMP_NOT_SESSION_START = "TIMESTAMP_NOT_SESSION_START"
    # strategy policy (INELIGIBLE)
    ZERO_VOLUME = "ZERO_VOLUME"
    # history limits (INSUFFICIENT_HISTORY)
    NO_BARS = "NO_BARS"
    HISTORY_STARTS_IN_WINDOW = "HISTORY_STARTS_IN_WINDOW"
    MISSING_SESSIONS = "MISSING_SESSIONS"
    LATEST_SESSION_MISSING = "LATEST_SESSION_MISSING"
    # calendar
    CALENDAR = "CALENDAR"


MALFORMED_ISSUES = frozenset(
    {
        HistoryIssue.WRONG_SYMBOL,
        HistoryIssue.WRONG_FEED,
        HistoryIssue.WRONG_ADJUSTMENT,
        HistoryIssue.NOT_CHRONOLOGICAL,
        HistoryIssue.DUPLICATE_SESSION,
        HistoryIssue.NOT_A_TRADING_SESSION,
        HistoryIssue.TIMESTAMP_NOT_SESSION_START,
    }
)
POLICY_ISSUES = frozenset({HistoryIssue.ZERO_VOLUME})


@dataclass(frozen=True, slots=True)
class HistoryRequirement:
    """What a caller needs: the last ``lookback_sessions`` completed sessions, from one
    feed and one adjustment. ``require_volume`` (default on, the initial liquid-equity
    policy) makes any zero-volume window session INELIGIBLE."""

    lookback_sessions: int
    feed: BarFeed
    adjustment: BarAdjustment
    require_volume: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.lookback_sessions, bool) or self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class HistoryFinding:
    issue: HistoryIssue
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HistoryAssessment:
    symbol: str
    status: HistoryStatus
    findings: tuple[HistoryFinding, ...]
    window: tuple[date, ...]
    """The required completed sessions, oldest first (empty if the calendar failed)."""
    bars: tuple[DailyBar, ...]
    """Exactly one bar per window session when ELIGIBLE; otherwise empty."""
    excluded: tuple[date, ...]
    """Sessions whose bars were dropped as incomplete (not closed when observed)."""

    @property
    def eligible(self) -> bool:
        return self.status is HistoryStatus.ELIGIBLE


def last_completed_session(calendar: TradingCalendar, now: datetime) -> date:
    """The most recent session whose close is at or before ``now``."""
    today = now.astimezone(MARKET_TZ).date()
    if calendar.is_trading_session(today) and calendar.session(today).close_at <= now:
        return today
    return calendar.previous_trading_session(today)


def required_window(calendar: TradingCalendar, now: datetime, lookback: int) -> tuple[date, ...]:
    """The last ``lookback`` completed sessions, oldest first. A caller requests bars for
    ``window[0]..window[-1]``."""
    if lookback < 1:
        raise ValueError("lookback must be positive")
    days = [last_completed_session(calendar, now)]
    while len(days) < lookback:
        days.append(calendar.previous_trading_session(days[-1]))
    return tuple(reversed(days))


def assess_history(
    symbol: str,
    bars: Sequence[DailyBar],
    *,
    calendar: TradingCalendar,
    now: datetime,
    requirement: HistoryRequirement,
) -> HistoryAssessment:
    try:
        window = required_window(calendar, now, requirement.lookback_sessions)
    except CalendarError as exc:
        return _result(symbol, [HistoryFinding(HistoryIssue.CALENDAR, str(exc))], (), (), ())
    first, last = window[0], window[-1]
    findings = _consistency(symbol, bars, requirement)

    usable: list[DailyBar] = []
    excluded: list[date] = []
    for bar in bars:
        if bar.session_date > last:
            excluded.append(bar.session_date)  # current or future session: not closed
            continue
        if bar.session_date < first:
            continue  # older than needed; ordering/identity already checked above
        try:
            if not calendar.is_trading_session(bar.session_date):
                findings.append(HistoryFinding(HistoryIssue.NOT_A_TRADING_SESSION,
                                               str(bar.session_date)))  # fmt: skip
                continue
            session = calendar.session(bar.session_date)
        except CalendarError as exc:  # coverage/staleness inside the window
            return _result(symbol, [HistoryFinding(HistoryIssue.CALENDAR, str(exc))], (), (), ())
        if bar.received_at < session.close_at:
            excluded.append(bar.session_date)  # observed before that session closed
            continue
        if bar.as_of.astimezone(MARKET_TZ).time() != time(0, 0):
            detail = f"{bar.session_date}: {bar.as_of.isoformat()}"
            findings.append(HistoryFinding(HistoryIssue.TIMESTAMP_NOT_SESSION_START, detail))
        if requirement.require_volume and bar.volume == 0:
            findings.append(HistoryFinding(HistoryIssue.ZERO_VOLUME, str(bar.session_date)))
        usable.append(bar)

    if not any(f.issue in MALFORMED_ISSUES for f in findings):
        findings += _coverage(bars, usable, window)
    by_session = {b.session_date: b for b in usable}
    complete = tuple(by_session[d] for d in window if d in by_session)
    return _result(symbol, findings, window, complete, tuple(sorted(set(excluded))))


def assess_histories(
    bars: Mapping[str, Sequence[DailyBar]],
    *,
    calendar: TradingCalendar,
    now: datetime,
    requirement: HistoryRequirement,
) -> dict[str, HistoryAssessment]:
    """Per-symbol assessment; one symbol's problem never affects another."""
    return {
        symbol: assess_history(symbol, series, calendar=calendar, now=now, requirement=requirement)
        for symbol, series in bars.items()
    }


# --------------------------------------------------------------------- internals
def _consistency(
    symbol: str, bars: Sequence[DailyBar], requirement: HistoryRequirement
) -> list[HistoryFinding]:
    out: list[HistoryFinding] = []
    previous: date | None = None
    for bar in bars:
        if bar.symbol != symbol:
            out.append(HistoryFinding(HistoryIssue.WRONG_SYMBOL, bar.symbol))
        if bar.feed != requirement.feed:
            out.append(HistoryFinding(HistoryIssue.WRONG_FEED, bar.feed))
        if bar.adjustment != requirement.adjustment:
            out.append(HistoryFinding(HistoryIssue.WRONG_ADJUSTMENT, bar.adjustment))
        if previous is not None:
            if bar.session_date == previous:
                out.append(HistoryFinding(HistoryIssue.DUPLICATE_SESSION, str(previous)))
            elif bar.session_date < previous:
                out.append(HistoryFinding(HistoryIssue.NOT_CHRONOLOGICAL,
                                          f"{bar.session_date} after {previous}"))  # fmt: skip
        previous = bar.session_date
    return out


def _coverage(
    bars: Sequence[DailyBar], usable: Sequence[DailyBar], window: tuple[date, ...]
) -> list[HistoryFinding]:
    present = {b.session_date for b in usable}
    missing = [d for d in window if d not in present]
    if not missing:
        return []
    if not present:
        return [HistoryFinding(HistoryIssue.NO_BARS, f"none for {window[0]}..{window[-1]}")]
    out: list[HistoryFinding] = []
    earliest = min(b.session_date for b in bars)
    leading = [d for d in missing if d < earliest]
    if leading:
        out.append(HistoryFinding(
            HistoryIssue.HISTORY_STARTS_IN_WINDOW,
            f"first bar {earliest}; {len(window) - len(leading)} of {len(window)} sessions",
        ))  # fmt: skip
    gaps = [d for d in missing if d >= earliest and d != window[-1]]
    if gaps:
        out.append(HistoryFinding(HistoryIssue.MISSING_SESSIONS,
                                  ", ".join(str(d) for d in gaps)))  # fmt: skip
    if window[-1] in missing:
        out.append(HistoryFinding(HistoryIssue.LATEST_SESSION_MISSING, str(window[-1])))
    return out


def _result(
    symbol: str,
    findings: list[HistoryFinding],
    window: tuple[date, ...],
    complete: tuple[DailyBar, ...],
    excluded: tuple[date, ...],
) -> HistoryAssessment:
    issues = {f.issue for f in findings}
    if HistoryIssue.CALENDAR in issues:
        status = HistoryStatus.CALENDAR_UNAVAILABLE
    elif issues & MALFORMED_ISSUES:
        status = HistoryStatus.MALFORMED
    elif issues - POLICY_ISSUES:
        status = HistoryStatus.INSUFFICIENT_HISTORY
    elif issues:
        status = HistoryStatus.INELIGIBLE
    else:
        status = HistoryStatus.ELIGIBLE
    return HistoryAssessment(
        symbol=symbol,
        status=status,
        findings=tuple(sorted(set(findings))),
        window=window,
        bars=complete if status is HistoryStatus.ELIGIBLE else (),
        excluded=excluded,
    )


__all__ = [
    "MALFORMED_ISSUES",
    "POLICY_ISSUES",
    "HistoryAssessment",
    "HistoryFinding",
    "HistoryIssue",
    "HistoryRequirement",
    "HistoryStatus",
    "assess_histories",
    "assess_history",
    "last_completed_session",
    "required_window",
]
