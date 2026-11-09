"""US equity trading and settlement calendar (ARCHITECTURE §6.3, M1 step 5).

Pure logic, no I/O. Two calendars are kept apart:

* **Trading sessions** — days the exchange is open, with open/close times (early
  closes included) and, when the provider supplies it, the authoritative
  per-session ``settlement_date``.
* **Settlement days** — settlement-eligible dates. Bank holidays can be trading days
  and exchange holidays can be settlement days.

Coverage is explicit: a date is *known* for a calendar kind only if an accepted
fetch's requested range contains it. Inside covered ranges, a missing session is a
holiday; outside, the answer is unknown and every API raises instead of guessing.
Coverage also expires (staleness) for dates that were still in the future when they
were last fetched.

Settlement dates are never computed by weekday arithmetic. ``settlement_date``
returns the provider's per-session value and/or the first settlement day after the
trade date from the settlement calendar; if both are available they must agree, and
if neither is, :class:`SettlementDateUnavailableError` is raised.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

MARKET_TZ: Final = ZoneInfo("America/New_York")
REGULAR_OPEN: Final = time(9, 30)
REGULAR_CLOSE: Final = time(16, 0)
ALPACA_DOCUMENTED_CALENDAR_END: Final = date(2029, 12, 31)
"""alpaca-py documents calendar data 'from 1970 to 2029' (ARCHITECTURE §3 V7)."""

MAX_SESSION_GAP_DAYS: Final = 10
"""Longest plausible run of closed days; a longer gap in covered data is contradictory."""

MAX_SETTLEMENT_LAG_DAYS: Final = 10
"""A settlement date further than this after the trade date is contradictory (T+1)."""


class CalendarKind(StrEnum):
    TRADING = "TRADING"
    SETTLEMENT = "SETTLEMENT"


class CalendarError(RuntimeError):
    """Base class: the calendar cannot answer safely."""


class CalendarCoverageError(CalendarError):
    """The requested date is outside accepted coverage."""


class StaleCalendarError(CalendarCoverageError):
    """The date is covered, but only by data older than the freshness limit."""


class NotATradingSessionError(CalendarError):
    pass


class SettlementDateUnavailableError(CalendarError):
    """No authoritative source can determine the settlement date."""


class ContradictoryCalendarError(CalendarError):
    pass


def _require_date(value: object, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{name} must be a datetime.date, got {type(value).__name__}")
    return value


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class TradingSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    settlement_date: date | None = None

    def __post_init__(self) -> None:
        _require_date(self.session_date, "session_date")
        object.__setattr__(self, "open_at", _require_aware(self.open_at, "open_at"))
        object.__setattr__(self, "close_at", _require_aware(self.close_at, "close_at"))
        if self.settlement_date is not None:
            _require_date(self.settlement_date, "settlement_date")

    @property
    def open_local(self) -> datetime:
        return self.open_at.astimezone(MARKET_TZ)

    @property
    def close_local(self) -> datetime:
        return self.close_at.astimezone(MARKET_TZ)

    @property
    def is_early_close(self) -> bool:
        return self.close_local.time() < REGULAR_CLOSE

    def problems(self) -> list[str]:
        d = self.session_date
        out: list[str] = []
        if d.weekday() >= 5:
            out.append(f"{d}: trading session on a weekend")
        if self.open_at >= self.close_at:
            out.append(f"{d}: open is not before close")
        if self.open_local.date() != d or self.close_local.date() != d:
            out.append(f"{d}: open/close not on the session date in America/New_York")
        if self.close_local.time() > REGULAR_CLOSE or self.open_local.time() < REGULAR_OPEN:
            out.append(f"{d}: session extends beyond regular hours")
        s = self.settlement_date
        if s is not None:
            if s <= d:
                out.append(f"{d}: settlement_date {s} is not after the trade date")
            elif (s - d).days > MAX_SETTLEMENT_LAG_DAYS:
                out.append(f"{d}: settlement_date {s} is implausibly late")
            if s.weekday() >= 5:
                out.append(f"{d}: settlement_date {s} is a weekend")
        return out


@dataclass(frozen=True, slots=True)
class CoverageRange:
    kind: CalendarKind
    start: date
    end: date
    fetched_at: datetime
    fetch_id: int | None = None

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True, slots=True)
class CalendarFetch:
    """One provider response for one calendar kind over an inclusive date range."""

    kind: CalendarKind
    start: date
    end: date
    fetched_at: datetime
    source: str
    sessions: tuple[TradingSession, ...] = ()
    settlement_days: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        _require_date(self.start, "start")
        _require_date(self.end, "end")
        object.__setattr__(self, "fetched_at", _require_aware(self.fetched_at, "fetched_at"))

    @property
    def row_count(self) -> int:
        return (
            len(self.sessions) if self.kind is CalendarKind.TRADING else len(self.settlement_days)
        )

    @property
    def payload_sha256(self) -> str:
        payload = {
            "kind": self.kind.value,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "sessions": [
                [
                    s.session_date.isoformat(),
                    s.open_at.isoformat(),
                    s.close_at.isoformat(),
                    None if s.settlement_date is None else s.settlement_date.isoformat(),
                ]
                for s in self.sessions
            ],
            "settlement_days": [d.isoformat() for d in self.settlement_days],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def problems(self) -> list[str]:
        """Internal consistency of the fetch itself (independent of stored data)."""
        out: list[str] = []
        if not self.source.strip():
            out.append("fetch source is blank")
        if self.start > self.end:
            out.append(f"range start {self.start} is after end {self.end}")
        if self.kind is CalendarKind.TRADING and self.settlement_days:
            out.append("TRADING fetch must not carry settlement days")
        if self.kind is CalendarKind.SETTLEMENT and self.sessions:
            out.append("SETTLEMENT fetch must not carry sessions")
        out.extend(self._session_problems())
        out.extend(self._settlement_day_problems())
        long_range = (self.end - self.start).days + 1 > MAX_SESSION_GAP_DAYS
        if long_range and self.row_count == 0:
            noun = "sessions" if self.kind is CalendarKind.TRADING else "settlement days"
            out.append(f"{self.kind.value} fetch has no {noun} over a long range")
        return out

    def _session_problems(self) -> list[str]:
        out: list[str] = []
        seen: set[date] = set()
        for s in self.sessions:
            if s.session_date in seen:
                out.append(f"{s.session_date}: duplicate session")
            seen.add(s.session_date)
            if not self.start <= s.session_date <= self.end:
                out.append(f"{s.session_date}: session outside requested range")
            out.extend(s.problems())
        return out

    def _settlement_day_problems(self) -> list[str]:
        out: list[str] = []
        seen: set[date] = set()
        for d in self.settlement_days:
            _require_date(d, "settlement day")
            if d in seen:
                out.append(f"{d}: duplicate settlement day")
            seen.add(d)
            if not self.start <= d <= self.end:
                out.append(f"{d}: settlement day outside requested range")
            if d.weekday() >= 5:
                out.append(f"{d}: settlement day on a weekend")
        return out


# ---------------------------------------------------------------- calendar data


@dataclass(frozen=True, slots=True)
class CalendarData:
    """Everything accepted so far: sessions, settlement days and coverage ranges."""

    sessions: Mapping[date, TradingSession] = field(default_factory=dict)
    settlement_days: frozenset[date] = frozenset()
    coverage: tuple[CoverageRange, ...] = ()

    def covered(self, kind: CalendarKind, d: date) -> bool:
        return any(r.kind is kind and r.contains(d) for r in self.coverage)

    def conflicts_with(self, fetch: CalendarFetch) -> list[str]:
        """Differences between a new fetch and already-accepted data in overlapping dates.

        Calendar history must never change silently (ARCHITECTURE §6.3 rule 6): a
        session added, removed or altered, or a settlement day added or removed, inside
        previously covered dates is a conflict. Filling in a previously unknown
        per-session settlement_date is allowed.
        """
        out: list[str] = []
        if fetch.kind is CalendarKind.TRADING:
            new = {s.session_date: s for s in fetch.sessions}
            for d in _days(fetch.start, fetch.end):
                if not self.covered(CalendarKind.TRADING, d):
                    continue
                old_s, new_s = self.sessions.get(d), new.get(d)
                if old_s is None and new_s is not None:
                    out.append(f"{d}: session appeared in previously covered range")
                elif old_s is not None and new_s is None:
                    out.append(f"{d}: session disappeared from previously covered range")
                elif old_s is not None and new_s is not None:
                    if (old_s.open_at, old_s.close_at) != (new_s.open_at, new_s.close_at):
                        out.append(f"{d}: session hours changed")
                    if (
                        old_s.settlement_date is not None
                        and old_s.settlement_date != new_s.settlement_date
                    ):
                        out.append(f"{d}: settlement_date changed or was withdrawn")
        else:
            new_days = set(fetch.settlement_days)
            for d in _days(fetch.start, fetch.end):
                if not self.covered(CalendarKind.SETTLEMENT, d):
                    continue
                if (d in self.settlement_days) != (d in new_days):
                    out.append(f"{d}: settlement-day status changed")
        return out

    def with_fetch(self, fetch: CalendarFetch, fetch_id: int | None = None) -> CalendarData:
        """Return a copy with ``fetch`` merged in (used to audit before persisting)."""
        sessions = dict(self.sessions)
        days = set(self.settlement_days)
        if fetch.kind is CalendarKind.TRADING:
            for incoming in fetch.sessions:
                old = sessions.get(incoming.session_date)
                merged = incoming
                if old is not None and incoming.settlement_date is None:
                    merged = TradingSession(
                        incoming.session_date,
                        incoming.open_at,
                        incoming.close_at,
                        old.settlement_date,
                    )
                sessions[merged.session_date] = merged
        else:
            days.update(fetch.settlement_days)
        rng = CoverageRange(fetch.kind, fetch.start, fetch.end, fetch.fetched_at, fetch_id)
        return CalendarData(sessions, frozenset(days), (*self.coverage, rng))

    def audit(self) -> list[str]:
        """Cross-calendar contradictions over all accepted data."""
        out: list[str] = []
        for d, s in sorted(self.sessions.items()):
            out.extend(s.problems())
            if not self.covered(CalendarKind.TRADING, d):
                out.append(f"{d}: session stored outside TRADING coverage")
            if s.settlement_date is None:
                continue
            explicit = s.settlement_date
            if self.covered(CalendarKind.SETTLEMENT, explicit) and (
                explicit not in self.settlement_days
            ):
                out.append(f"{d}: settlement_date {explicit} is not a settlement day")
            derived = _first_settlement_day_after(self, d)
            if derived is not None and derived != explicit:
                out.append(
                    f"{d}: settlement_date {explicit} disagrees with settlement calendar "
                    f"({derived})"
                )
        for d in sorted(self.settlement_days):
            if d.weekday() >= 5:
                out.append(f"{d}: settlement day on a weekend")
        return out


def _first_settlement_day_after(data: CalendarData, trade_date: date) -> date | None:
    """First settlement day strictly after ``trade_date``; None if coverage runs out."""
    d = trade_date
    for _ in range(MAX_SETTLEMENT_LAG_DAYS):
        d += timedelta(days=1)
        if not data.covered(CalendarKind.SETTLEMENT, d):
            return None
        if d in data.settlement_days:
            return d
    return None


# ---------------------------------------------------------------- coverage report


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class CoverageReport:
    start: date
    end: date
    missing: Mapping[CalendarKind, tuple[DateRange, ...]]
    stale: Mapping[CalendarKind, tuple[DateRange, ...]]

    @property
    def complete(self) -> bool:
        return not any(self.missing.values()) and not any(self.stale.values())

    def describe(self) -> list[str]:
        out: list[str] = []
        for label, table in (("missing", self.missing), ("stale", self.stale)):
            for kind, ranges in table.items():
                out.extend(f"{kind.value} {label}: {r.start}..{r.end}" for r in ranges)
        return out


def _compress(days: Sequence[date]) -> tuple[DateRange, ...]:
    ranges: list[DateRange] = []
    for d in days:
        if ranges and ranges[-1].end + timedelta(days=1) == d:
            ranges[-1] = DateRange(ranges[-1].start, d)
        else:
            ranges.append(DateRange(d, d))
    return tuple(ranges)


@dataclass(frozen=True, slots=True)
class HorizonStatus:
    trading_horizon: date | None
    settlement_horizon: date | None
    days_remaining: int | None
    provider_documented_end: date
    warning: bool
    """True when contiguous fresh coverage ends within the warning window."""


# ------------------------------------------------------------------ the calendar


class TradingCalendar:
    """Read-only view over accepted calendar data, evaluated at ``as_of``."""

    def __init__(self, data: CalendarData, *, as_of: datetime, max_age: timedelta) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self._data = data
        self._as_of = _require_aware(as_of, "as_of")
        self._max_age = max_age
        problems = data.audit()
        if problems:
            raise ContradictoryCalendarError("; ".join(problems[:5]))

    @property
    def as_of(self) -> datetime:
        return self._as_of

    # -------------------------------------------------------------- coverage
    def _status(self, kind: CalendarKind, d: date) -> str:
        """'ok', 'missing' or 'stale' for one date."""
        fetched = [r.fetched_at for r in self._data.coverage if r.kind is kind and r.contains(d)]
        if not fetched:
            return "missing"
        latest = max(fetched)
        if self._as_of - latest <= self._max_age:
            return "ok"
        # Dates already in the past when fetched are historical record, not forecasts.
        if d < latest.astimezone(MARKET_TZ).date():
            return "ok"
        return "stale"

    def _require(self, kind: CalendarKind, d: date) -> None:
        status = self._status(kind, d)
        if status == "missing":
            raise CalendarCoverageError(f"{kind.value} calendar does not cover {d}")
        if status == "stale":
            raise StaleCalendarError(f"{kind.value} calendar data for {d} is stale")

    def calendar_coverage(self, start: date, end: date) -> CoverageReport:
        _require_date(start, "start")
        _require_date(end, "end")
        if start > end:
            raise ValueError("start must not be after end")
        missing: dict[CalendarKind, tuple[DateRange, ...]] = {}
        stale: dict[CalendarKind, tuple[DateRange, ...]] = {}
        for kind in CalendarKind:
            statuses = [(d, self._status(kind, d)) for d in _days(start, end)]
            missing[kind] = _compress([d for d, s in statuses if s == "missing"])
            stale[kind] = _compress([d for d, s in statuses if s == "stale"])
        return CoverageReport(start, end, missing, stale)

    def require_coverage(self, start: date, end: date) -> None:
        report = self.calendar_coverage(start, end)
        if not report.complete:
            raise CalendarCoverageError("; ".join(report.describe()[:5]))

    def require_future_coverage(self, days: int) -> None:
        """Both calendars must be fresh from today (market tz) through ``days`` ahead."""
        if days < 1:
            raise ValueError("days must be positive")
        today = self._as_of.astimezone(MARKET_TZ).date()
        self.require_coverage(today, today + timedelta(days=days))

    def horizon_status(self, warning_days: int) -> HorizonStatus:
        today = self._as_of.astimezone(MARKET_TZ).date()

        def horizon(kind: CalendarKind) -> date | None:
            if self._status(kind, today) != "ok":
                return None
            d = today
            while self._status(kind, d + timedelta(days=1)) == "ok":
                d += timedelta(days=1)
            return d

        trading, settlement = horizon(CalendarKind.TRADING), horizon(CalendarKind.SETTLEMENT)
        ends = [h for h in (trading, settlement) if h is not None]
        remaining = None if len(ends) < 2 else (min(ends) - today).days
        return HorizonStatus(
            trading_horizon=trading,
            settlement_horizon=settlement,
            days_remaining=remaining,
            provider_documented_end=ALPACA_DOCUMENTED_CALENDAR_END,
            warning=remaining is None or remaining < warning_days,
        )

    # ---------------------------------------------------------------- sessions
    def is_trading_session(self, d: date) -> bool:
        _require_date(d, "date")
        self._require(CalendarKind.TRADING, d)
        return d in self._data.sessions

    def session(self, d: date) -> TradingSession:
        if not self.is_trading_session(d):
            raise NotATradingSessionError(f"{d} is not a trading session")
        return self._data.sessions[d]

    def _step(self, d: date, direction: int) -> date:
        _require_date(d, "date")
        candidate = d
        for _ in range(MAX_SESSION_GAP_DAYS):
            candidate += timedelta(days=direction)
            self._require(CalendarKind.TRADING, candidate)
            if candidate in self._data.sessions:
                return candidate
        raise ContradictoryCalendarError(
            f"no trading session within {MAX_SESSION_GAP_DAYS} days of {d}"
        )

    def next_trading_session(self, d: date) -> date:
        """First trading session strictly after ``d``."""
        return self._step(d, 1)

    def previous_trading_session(self, d: date) -> date:
        """Last trading session strictly before ``d``."""
        return self._step(d, -1)

    # -------------------------------------------------------------- settlement
    def is_settlement_day(self, d: date) -> bool:
        _require_date(d, "date")
        self._require(CalendarKind.SETTLEMENT, d)
        return d in self._data.settlement_days

    def settlement_date(self, trade_date: date) -> date:
        """Authoritative settlement date for a trade executed in session ``trade_date``.

        Sources, in order of authority: the provider's per-session ``settlement_date``
        and the first settlement day after the trade date on the settlement calendar
        (T+1 in settlement days). If both exist they must agree. Never guessed.
        """
        session = self.session(trade_date)
        explicit = session.settlement_date
        derived = self._derived_settlement(trade_date)
        if explicit is not None:
            if derived is not None and derived != explicit:
                raise ContradictoryCalendarError(
                    f"{trade_date}: provider settlement_date {explicit} != settlement "
                    f"calendar {derived}"
                )
            self._require_settlement_fresh(explicit)
            return explicit
        if derived is None:
            raise SettlementDateUnavailableError(
                f"{trade_date}: no authoritative settlement date (per-session value absent "
                "and settlement calendar does not cover the following days)"
            )
        return derived

    def _require_settlement_fresh(self, settles_on: date) -> None:
        # A per-session date is authoritative on its own; if settlement-calendar data
        # exists for that date it must be fresh and agree.
        status = self._status(CalendarKind.SETTLEMENT, settles_on)
        if status == "stale":
            raise StaleCalendarError(f"settlement calendar data for {settles_on} is stale")
        if status == "ok" and settles_on not in self._data.settlement_days:
            raise ContradictoryCalendarError(f"{settles_on} is not a settlement day")

    def _derived_settlement(self, trade_date: date) -> date | None:
        d = trade_date
        for _ in range(MAX_SETTLEMENT_LAG_DAYS):
            d += timedelta(days=1)
            status = self._status(CalendarKind.SETTLEMENT, d)
            if status == "missing":
                return None
            if status == "stale":
                raise StaleCalendarError(f"settlement calendar data for {d} is stale")
            if d in self._data.settlement_days:
                return d
        raise ContradictoryCalendarError(
            f"no settlement day within {MAX_SETTLEMENT_LAG_DAYS} days after {trade_date}"
        )
