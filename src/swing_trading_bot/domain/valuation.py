"""Price-mark freshness (ARCHITECTURE §6.6, U7). Pure logic.

A mark is usable only if every rule holds; otherwise market value, unrealized P&L
and equity are *unknown* and new entries are blocked. Nothing here falls back to a
cost price, a previous mark or ``Position.current_price``.

Rules (values from ``config/risk.yaml``):

* the mark exists and is not from the future (``as_of <= received_at + skew`` and
  ``as_of <= now + skew``);
* it was received recently (``now - received_at <= max_sync_age``);
* during a regular session: ``as_of`` is within the session and no older than
  ``max_age_in_session`` (15 min);
* outside a session: ``as_of`` is on the most recent completed session's date (market
  tz) and not before that session's open — a last trade or that session's daily bar;
* if the calendar cannot answer, freshness cannot be verified (blocked).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from swing_trading_bot.domain.calendar import (
    MARKET_TZ,
    CalendarError,
    TradingCalendar,
    TradingSession,
)
from swing_trading_bot.domain.enums import MarkSource
from swing_trading_bot.domain.models import PriceMark


class MarkStatus(StrEnum):
    FRESH = "FRESH"
    MISSING = "MISSING"
    STALE = "STALE"
    FUTURE = "FUTURE"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True, slots=True)
class MarkFreshnessPolicy:
    max_age_in_session: timedelta
    max_future_skew: timedelta
    max_receipt_age: timedelta

    @classmethod
    def from_seconds(
        cls, *, in_session: int, future_skew: int, receipt_age: int
    ) -> MarkFreshnessPolicy:
        return cls(
            max_age_in_session=timedelta(seconds=in_session),
            max_future_skew=timedelta(seconds=future_skew),
            max_receipt_age=timedelta(seconds=receipt_age),
        )


@dataclass(frozen=True, slots=True)
class MarkAssessment:
    status: MarkStatus
    reason: str

    @property
    def fresh(self) -> bool:
        return self.status is MarkStatus.FRESH


def assess_mark(
    mark: PriceMark | None,
    *,
    now: datetime,
    calendar: TradingCalendar | None,
    policy: MarkFreshnessPolicy,
) -> MarkAssessment:
    if mark is None:
        return MarkAssessment(MarkStatus.MISSING, "no price mark")
    basic = _basic_problem(mark, now, policy)
    if basic is not None:
        return basic
    if calendar is None:
        return MarkAssessment(MarkStatus.UNVERIFIABLE, "no calendar available")
    try:
        return _session_assessment(mark, now, calendar, policy)
    except CalendarError as exc:
        return MarkAssessment(MarkStatus.UNVERIFIABLE, f"calendar: {exc}")


def _basic_problem(
    mark: PriceMark, now: datetime, policy: MarkFreshnessPolicy
) -> MarkAssessment | None:
    skew = policy.max_future_skew
    if mark.as_of > mark.received_at + skew or mark.as_of > now + skew:
        return MarkAssessment(MarkStatus.FUTURE, "mark timestamp is in the future")
    if now - mark.received_at > policy.max_receipt_age:
        return MarkAssessment(MarkStatus.STALE, "mark not refreshed within max sync age")
    return None


def _session_assessment(
    mark: PriceMark, now: datetime, calendar: TradingCalendar, policy: MarkFreshnessPolicy
) -> MarkAssessment:
    today = now.astimezone(MARKET_TZ).date()
    reference: TradingSession | None = None
    if calendar.is_trading_session(today):
        session = calendar.session(today)
        if session.open_at <= now <= session.close_at:
            if mark.as_of < session.open_at:
                return MarkAssessment(MarkStatus.STALE, "mark predates the current session")
            if now - mark.as_of > policy.max_age_in_session:
                return MarkAssessment(MarkStatus.STALE, "mark older than in-session limit")
            return MarkAssessment(MarkStatus.FRESH, "in-session mark")
        if now > session.close_at:
            reference = session
    if reference is None:
        reference = calendar.session(calendar.previous_trading_session(today))
    if mark.as_of.astimezone(MARKET_TZ).date() != reference.session_date:
        return MarkAssessment(MarkStatus.STALE, "mark is not from the last completed session")
    if mark.source is not MarkSource.SESSION_CLOSE_BAR and mark.as_of < reference.open_at:
        return MarkAssessment(MarkStatus.STALE, "mark predates the last session open")
    return MarkAssessment(MarkStatus.FRESH, "last-session mark")
