"""Historical data contract for the support/resistance strategy (Step 16.14, Phase A).

Pure functions; no I/O. Input is what the existing
:class:`~swing_trading_bot.broker.ports.BarSource` returns
(:class:`~swing_trading_bot.broker.models.DailyBar`), plus a session calendar and an
explicit :class:`CorporateActions` record.

**Contract.**

* *Identity and ordering.* One symbol per series; bars strictly increasing by session; no
  duplicates; every bar on a trading session of the calendar. Any violation rejects the
  whole series (``MALFORMED``): nothing is sorted, de-duplicated or repaired.
* *Feed and provenance.* One feed per series (``sip`` by default; the consolidated tape).
  The series records feed, adjustment, first/last session, bar count and a content digest.
  A bar's close is an **observation** from that feed, never an official closing price,
  and nothing here writes a reference close.
* *Completeness.* A bar observed before its session's close (``received_at < close_at``)
  is provisional: it is excluded and its session counts as missing. Source timestamps
  must be the session start (midnight America/New_York, the verified Alpaca convention).
* *Missing sessions.* Never filled, interpolated or carried forward. A decision window
  that contains a missing session is ``INSUFFICIENT`` and produces no signal.
* *Corporate actions.* Execution prices are **raw** bars (what actually traded). Strategy
  windows are **point-in-time split-adjusted**: for a decision on session ``D`` a bar on
  session ``s`` is divided by the splits effective in ``(s, D]`` only, so the most recent
  bar is always in raw (tradable) terms and no split announced after ``D`` leaks into the
  decision. Provider ``adjustment=split`` series are restated retroactively, so they are
  used only to *derive* split events (:func:`derive_split_events`), never as decision
  input. Cash dividends are never price-adjusted; they are booked as cash (backtest).
* *Corporate-action completeness (Step 16.14A).* Every series needs a
  :class:`CorporateActions` record whose split coverage (and, under
  ``DividendPolicy.REQUIRE_COVERAGE``, dividend coverage) spans every accepted bar. An
  empty event list means "none occurred" **only** inside an attested coverage window;
  no record, a coverage gap, an event outside its window, an invalid or conflicting
  event, or a recorded split the raw prices do not show rejects the series. An
  unrecorded overnight move that matches a plausible split ratio is treated
  asymmetrically so it can never flatter results: a *rise* (an unrecorded reverse split
  would fabricate a gain) rejects the series; a *drop* (a genuine crash must not be
  excluded — that would be survivorship bias) keeps the series, marks the session as a
  suspect discontinuity that no decision window may span, and is booked as a real gap.
* *Look-ahead.* :meth:`SymbolSeries.window` returns only bars on or before the decision
  session; the decision time is that session's close.
"""

from __future__ import annotations

import hashlib
import itertools
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from swing_trading_bot.broker.models import BarFeed, DailyBar
from swing_trading_bot.domain.calendar import MARKET_TZ, TradingSession
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, ONE, ZERO

#: Largest term of a derived split ratio (e.g. 1-for-50) ...
MAX_SPLIT_TERM: Final = 50
#: ... and the largest *smaller* term (3:2, 5:4 and 7:4 are plausible; 32:33 is not).
MAX_SPLIT_MINOR_TERM: Final = 4
#: Relative tolerance for raw/adjusted ratio comparisons (provider rounds adjusted prices).
SPLIT_RATIO_TOLERANCE: Final = Decimal("0.005")


class DataIssue(StrEnum):
    # series faults (the series is rejected)
    WRONG_SYMBOL = "WRONG_SYMBOL"
    WRONG_FEED = "WRONG_FEED"
    WRONG_ADJUSTMENT = "WRONG_ADJUSTMENT"
    NOT_CHRONOLOGICAL = "NOT_CHRONOLOGICAL"
    DUPLICATE_SESSION = "DUPLICATE_SESSION"
    NOT_A_TRADING_SESSION = "NOT_A_TRADING_SESSION"
    TIMESTAMP_NOT_SESSION_START = "TIMESTAMP_NOT_SESSION_START"
    SPLIT_EVENT_INVALID = "SPLIT_EVENT_INVALID"
    SPLIT_RATIO_UNRESOLVED = "SPLIT_RATIO_UNRESOLVED"
    ADJUSTED_COVERAGE_MISMATCH = "ADJUSTED_COVERAGE_MISMATCH"
    CORPORATE_ACTIONS_MISSING = "CORPORATE_ACTIONS_MISSING"
    CORPORATE_ACTIONS_SYMBOL_MISMATCH = "CORPORATE_ACTIONS_SYMBOL_MISMATCH"
    SPLIT_COVERAGE_MISSING = "SPLIT_COVERAGE_MISSING"
    SPLIT_COVERAGE_GAP = "SPLIT_COVERAGE_GAP"
    DIVIDEND_COVERAGE_MISSING = "DIVIDEND_COVERAGE_MISSING"
    DIVIDEND_COVERAGE_GAP = "DIVIDEND_COVERAGE_GAP"
    CORPORATE_ACTION_OUTSIDE_COVERAGE = "CORPORATE_ACTION_OUTSIDE_COVERAGE"
    DIVIDEND_EVENT_INVALID = "DIVIDEND_EVENT_INVALID"
    CORPORATE_ACTION_CONFLICT = "CORPORATE_ACTION_CONFLICT"
    SPLIT_NOT_REFLECTED_IN_PRICES = "SPLIT_NOT_REFLECTED_IN_PRICES"
    SPLIT_SUSPECTED_UNRECORDED = "SPLIT_SUSPECTED_UNRECORDED"
    # informational (not fatal)
    DIVIDENDS_EXCLUDED = "DIVIDENDS_EXCLUDED"
    PRICE_DISCONTINUITY_UNEXPLAINED = "PRICE_DISCONTINUITY_UNEXPLAINED"
    # exclusions (the bar is dropped; its session counts as missing)
    INCOMPLETE_BAR = "INCOMPLETE_BAR"
    OUTSIDE_CALENDAR = "OUTSIDE_CALENDAR"


FATAL_ISSUES: Final = frozenset(
    {
        DataIssue.WRONG_SYMBOL,
        DataIssue.WRONG_FEED,
        DataIssue.WRONG_ADJUSTMENT,
        DataIssue.NOT_CHRONOLOGICAL,
        DataIssue.DUPLICATE_SESSION,
        DataIssue.NOT_A_TRADING_SESSION,
        DataIssue.TIMESTAMP_NOT_SESSION_START,
        DataIssue.SPLIT_EVENT_INVALID,
        DataIssue.SPLIT_RATIO_UNRESOLVED,
        DataIssue.ADJUSTED_COVERAGE_MISMATCH,
        DataIssue.CORPORATE_ACTIONS_MISSING,
        DataIssue.CORPORATE_ACTIONS_SYMBOL_MISMATCH,
        DataIssue.SPLIT_COVERAGE_MISSING,
        DataIssue.SPLIT_COVERAGE_GAP,
        DataIssue.DIVIDEND_COVERAGE_MISSING,
        DataIssue.DIVIDEND_COVERAGE_GAP,
        DataIssue.CORPORATE_ACTION_OUTSIDE_COVERAGE,
        DataIssue.DIVIDEND_EVENT_INVALID,
        DataIssue.CORPORATE_ACTION_CONFLICT,
        DataIssue.SPLIT_NOT_REFLECTED_IN_PRICES,
        DataIssue.SPLIT_SUSPECTED_UNRECORDED,
    }
)
#: An unrecorded overnight move is a suspected split only if it is at least this large.
SUSPECT_SPLIT_MIN_MOVE: Final = Decimal("0.2")
#: A recorded split of at least 3:2 (or 2:3) must show in raw prices within this band.
SPLIT_REFLECTION_BAND: Final = (Decimal("0.75"), Decimal("1.3334"))


@dataclass(frozen=True, slots=True, order=True)
class DataFinding:
    issue: DataIssue
    detail: str = ""


class DataContractError(ValueError):
    """Invalid calendar, bar or split input to the data contract."""


# ===================================================================== calendar


class SessionCalendar:
    """An ordered, gap-free list of trading sessions (from the cached Alpaca calendar or
    the offline fixture). Used for replay order, windows and settlement dates."""

    __slots__ = ("_by_date", "_dates", "_sessions")

    def __init__(self, sessions: Iterable[TradingSession]) -> None:
        ordered = tuple(sessions)
        if not ordered:
            raise DataContractError("calendar has no sessions")
        dates = tuple(s.session_date for s in ordered)
        if any(b <= a for a, b in itertools.pairwise(dates)):
            raise DataContractError("calendar sessions must be strictly increasing")
        for s in ordered:
            if s.close_at <= s.open_at:
                raise DataContractError(f"session {s.session_date} closes before it opens")
        self._sessions = ordered
        self._dates = dates
        self._by_date = {s.session_date: s for s in ordered}

    @property
    def dates(self) -> tuple[date, ...]:
        return self._dates

    @property
    def first(self) -> date:
        return self._dates[0]

    @property
    def last(self) -> date:
        return self._dates[-1]

    def covers(self, d: date) -> bool:
        return self.first <= d <= self.last

    def is_session(self, d: date) -> bool:
        return d in self._by_date

    def session(self, d: date) -> TradingSession:
        try:
            return self._by_date[d]
        except KeyError:
            raise DataContractError(f"{d} is not a session of the calendar") from None

    def index(self, d: date) -> int:
        i = bisect_left(self._dates, d)
        if i == len(self._dates) or self._dates[i] != d:
            raise DataContractError(f"{d} is not a session of the calendar")
        return i

    def next_session(self, d: date) -> date | None:
        i = self.index(d)
        return self._dates[i + 1] if i + 1 < len(self._dates) else None

    def window(self, end: date, count: int) -> tuple[date, ...] | None:
        """The ``count`` sessions ending at ``end`` (inclusive), oldest first, or None if
        the calendar does not reach back that far."""
        if count < 1:
            raise DataContractError("count must be positive")
        i = self.index(end)
        if i + 1 < count:
            return None
        return self._dates[i + 1 - count : i + 1]

    def between(self, start: date, end: date) -> tuple[date, ...]:
        lo = bisect_left(self._dates, start)
        hi = bisect_left(self._dates, end)
        if hi < len(self._dates) and self._dates[hi] == end:
            hi += 1
        return self._dates[lo:hi]

    def settlement_date(self, d: date) -> date | None:
        """The session's settlement date from the calendar, or the next session when the
        calendar does not carry one (an approximation used only offline)."""
        s = self.session(d)
        return s.settlement_date if s.settlement_date is not None else self.next_session(d)


# ===================================================================== records


def _non_negative_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= ZERO


def _positive(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise DataContractError(f"{name} must be a finite, positive Decimal")
    return value


@dataclass(frozen=True, slots=True)
class StrategyBar:
    """A completed session's OHLCV in one price basis. Prices are exact Decimals (they
    may carry more than four places after a point-in-time split adjustment); volume is a
    Decimal because a split adjustment can make it fractional."""

    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        o = _positive(self.open, "open")
        h = _positive(self.high, "high")
        lo = _positive(self.low, "low")
        c = _positive(self.close, "close")
        if not (lo <= o <= h and lo <= c <= h):
            raise DataContractError("inconsistent OHLC: need low <= open, close <= high")
        if not _non_negative_decimal(self.volume):
            raise DataContractError("volume must be a finite, non-negative Decimal")

    @property
    def dollar_volume(self) -> Decimal:
        return self.close * self.volume

    def scaled(self, factor: Fraction) -> StrategyBar:
        """Prices divided by ``factor`` and volume multiplied by it (a split adjustment)."""
        if factor == 1:
            return self
        num, den = Decimal(factor.numerator), Decimal(factor.denominator)

        def p(x: Decimal) -> Decimal:
            return DECIMAL_CONTEXT.divide(x * den, num)

        return StrategyBar(
            self.symbol, self.session_date, p(self.open), p(self.high), p(self.low),
            p(self.close), DECIMAL_CONTEXT.divide(self.volume * num, den),
        )  # fmt: skip


@dataclass(frozen=True, slots=True, order=True)
class SplitEvent:
    """``new_shares`` for ``old_shares`` effective at the open of ``effective_session``
    (the first session on the post-split basis). 2-for-1 is (2, 1); 1-for-10 is (1, 10)."""

    symbol: str
    effective_session: date
    new_shares: int
    old_shares: int
    source: str = "manual"

    def __post_init__(self) -> None:
        for name in ("new_shares", "old_shares"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise DataContractError(f"{name} must be a positive integer")
        if self.new_shares == self.old_shares:
            raise DataContractError("a split must change the share count")
        if isinstance(self.effective_session, datetime):
            raise DataContractError("effective_session must be a date")

    @property
    def ratio(self) -> Fraction:
        return Fraction(self.new_shares, self.old_shares)


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    """An attestation that ``source`` was complete for ``[start, end]``: an event list
    with nothing in this window means no such event occurred in it."""

    start: date
    end: date
    source: str

    def __post_init__(self) -> None:
        if isinstance(self.start, datetime) or isinstance(self.end, datetime):
            raise DataContractError("coverage bounds must be dates")
        if self.end < self.start:
            raise DataContractError("coverage end precedes start")
        if not self.source:
            raise DataContractError("coverage needs a source")

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True, slots=True, order=True)
class CashDividend:
    """A cash dividend of ``amount`` per share held at the close before ``ex_session``,
    paid on ``pay_date``."""

    symbol: str
    ex_session: date
    pay_date: date
    amount: Decimal
    source: str = "manual"

    def __post_init__(self) -> None:
        if isinstance(self.ex_session, datetime) or isinstance(self.pay_date, datetime):
            raise DataContractError("dividend dates must be dates")
        _positive(self.amount, "dividend amount")
        if self.pay_date < self.ex_session:
            raise DataContractError("pay_date precedes ex_session")


class DividendPolicy(StrEnum):
    REQUIRE_COVERAGE = "REQUIRE_COVERAGE"
    """Default: dividends are booked; their coverage must be attested (fail closed)."""
    PRICE_RETURN_ONLY = "PRICE_RETURN_ONLY"
    """Explicit opt-out: dividends are excluded and the result is labelled price-return."""


@dataclass(frozen=True, slots=True)
class CorporateActions:
    """The corporate-action record for one symbol: events plus the windows in which the
    event lists are attested complete. ``None`` coverage means "unknown", never "none"."""

    symbol: str
    splits: tuple[SplitEvent, ...] = ()
    dividends: tuple[CashDividend, ...] = ()
    split_coverage: CoverageWindow | None = None
    dividend_coverage: CoverageWindow | None = None


class PriceBasis(StrEnum):
    RAW = "RAW"
    POINT_IN_TIME_SPLIT_ADJUSTED = "POINT_IN_TIME_SPLIT_ADJUSTED"


@dataclass(frozen=True, slots=True)
class SeriesProvenance:
    symbol: str
    feed: BarFeed
    adjustment: str
    first_session: date | None
    last_session: date | None
    bar_count: int
    digest: str
    """SHA-256 over the canonical raw bars, corporate actions and coverage."""
    dividend_policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE


class WindowStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INSUFFICIENT = "INSUFFICIENT"


class WindowIssue(StrEnum):
    CALENDAR_TOO_SHORT = "CALENDAR_TOO_SHORT"
    NOT_A_SESSION = "NOT_A_SESSION"
    HISTORY_STARTS_IN_WINDOW = "HISTORY_STARTS_IN_WINDOW"
    MISSING_SESSIONS = "MISSING_SESSIONS"
    DECISION_SESSION_MISSING = "DECISION_SESSION_MISSING"
    CORPORATE_ACTION_SUSPECT = "CORPORATE_ACTION_SUSPECT"


@dataclass(frozen=True, slots=True)
class BarWindow:
    """Exactly the data a decision may use: the ``len(bars)`` sessions ending at
    ``decision_session``, point-in-time split-adjusted as of that session."""

    symbol: str
    decision_session: date
    decision_at: datetime
    """The decision session's close: the earliest time this window is complete."""
    bars: tuple[StrategyBar, ...]
    basis: PriceBasis
    splits_applied: tuple[SplitEvent, ...]
    feed: BarFeed

    def __post_init__(self) -> None:
        if not self.bars or self.bars[-1].session_date != self.decision_session:
            raise DataContractError("a window must end at its decision session")
        if any(b.session_date > self.decision_session for b in self.bars):
            raise DataContractError("a window cannot contain bars after the decision session")
        if any(b.symbol != self.symbol for b in self.bars):
            raise DataContractError("a window holds one symbol")


@dataclass(frozen=True, slots=True)
class WindowResult:
    status: WindowStatus
    issue: WindowIssue | None
    detail: str
    window: BarWindow | None


# ===================================================================== series


@dataclass(frozen=True, slots=True)
class SymbolSeries:
    """A validated raw daily series plus its split events. Construct with
    :func:`prepare_series`."""

    symbol: str
    feed: BarFeed
    bars: tuple[StrategyBar, ...]
    splits: tuple[SplitEvent, ...]
    provenance: SeriesProvenance
    _index: Mapping[date, int] = field(repr=False, compare=False)
    dividends: tuple[CashDividend, ...] = ()
    suspect_sessions: frozenset[date] = frozenset()
    """Sessions opening with an unexplained split-like price drop (no window spans one)."""

    @property
    def dividends_included(self) -> bool:
        return self.provenance.dividend_policy is DividendPolicy.REQUIRE_COVERAGE

    def dividends_on(self, session: date) -> tuple[CashDividend, ...]:
        return tuple(d for d in self.dividends if d.ex_session == session)

    def bar(self, session: date) -> StrategyBar | None:
        i = self._index.get(session)
        return None if i is None else self.bars[i]

    def splits_on(self, session: date) -> tuple[SplitEvent, ...]:
        return tuple(s for s in self.splits if s.effective_session == session)

    def last_bar_on_or_before(self, session: date) -> StrategyBar | None:
        dates = [b.session_date for b in self.bars]
        i = bisect_left(dates, session)
        if i < len(dates) and dates[i] == session:
            return self.bars[i]
        return self.bars[i - 1] if i > 0 else None

    def window(self, calendar: SessionCalendar, decision_session: date, count: int) -> WindowResult:
        """The point-in-time window for a decision after ``decision_session`` closes."""
        if not calendar.is_session(decision_session):
            return WindowResult(WindowStatus.INSUFFICIENT, WindowIssue.NOT_A_SESSION,
                                str(decision_session), None)  # fmt: skip
        sessions = calendar.window(decision_session, count)
        if sessions is None:
            return WindowResult(WindowStatus.INSUFFICIENT, WindowIssue.CALENDAR_TOO_SHORT,
                                f"{count} sessions before {decision_session}", None)  # fmt: skip
        missing = [d for d in sessions if d not in self._index]
        if missing:
            return _missing_result(self, sessions, missing, decision_session)
        suspect = sorted(d for d in sessions[1:] if d in self.suspect_sessions)
        if suspect:
            return WindowResult(WindowStatus.INSUFFICIENT, WindowIssue.CORPORATE_ACTION_SUSPECT,
                                ", ".join(str(d) for d in suspect), None)  # fmt: skip
        applied = tuple(s for s in self.splits if sessions[0] < s.effective_session
                        and s.effective_session <= decision_session)  # fmt: skip
        out: list[StrategyBar] = []
        for d in sessions:
            factor = Fraction(1)
            for s in applied:
                if d < s.effective_session:
                    factor *= s.ratio
            out.append(self.bars[self._index[d]].scaled(factor))
        basis = PriceBasis.POINT_IN_TIME_SPLIT_ADJUSTED if applied else PriceBasis.RAW
        win = BarWindow(self.symbol, decision_session, calendar.session(decision_session).close_at,
                        tuple(out), basis, applied, self.feed)  # fmt: skip
        return WindowResult(WindowStatus.COMPLETE, None, "", win)


def _missing_result(
    series: SymbolSeries, sessions: Sequence[date], missing: Sequence[date], decision: date
) -> WindowResult:
    if decision in missing:
        issue = WindowIssue.DECISION_SESSION_MISSING
    elif series.provenance.first_session is None or series.provenance.first_session > sessions[0]:
        issue = WindowIssue.HISTORY_STARTS_IN_WINDOW
    else:
        issue = WindowIssue.MISSING_SESSIONS
    shown = ", ".join(str(d) for d in missing[:5]) + (" ..." if len(missing) > 5 else "")
    return WindowResult(WindowStatus.INSUFFICIENT, issue, shown, None)


@dataclass(frozen=True, slots=True)
class SeriesPreparation:
    symbol: str
    series: SymbolSeries | None
    """None when a fatal finding rejected the series."""
    findings: tuple[DataFinding, ...]

    @property
    def accepted(self) -> bool:
        return self.series is not None


def prepare_series(
    symbol: str,
    raw_bars: Sequence[DailyBar],
    *,
    calendar: SessionCalendar,
    corporate_actions: CorporateActions | None,
    dividend_policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE,
    feed: BarFeed = "sip",
) -> SeriesPreparation:
    """Validate a raw daily series and its corporate actions. Never repairs data.

    ``corporate_actions`` is required: ``None`` (no record) rejects the series rather
    than being read as "no corporate actions"."""
    findings: list[DataFinding] = []
    kept: list[StrategyBar] = []
    previous: date | None = None
    for bar in raw_bars:
        findings += _bar_identity(symbol, bar, feed, previous)
        previous = bar.session_date
        if not calendar.covers(bar.session_date):
            findings.append(DataFinding(DataIssue.OUTSIDE_CALENDAR, str(bar.session_date)))
            continue
        if not calendar.is_session(bar.session_date):
            findings.append(DataFinding(DataIssue.NOT_A_TRADING_SESSION, str(bar.session_date)))
            continue
        if bar.as_of.astimezone(MARKET_TZ).time() != time(0, 0):
            detail = f"{bar.session_date}: {bar.as_of.isoformat()}"
            findings.append(DataFinding(DataIssue.TIMESTAMP_NOT_SESSION_START, detail))
        if bar.received_at < calendar.session(bar.session_date).close_at:
            findings.append(DataFinding(DataIssue.INCOMPLETE_BAR, str(bar.session_date)))
            continue
        kept.append(StrategyBar(bar.symbol, bar.session_date, bar.open, bar.high, bar.low,
                                bar.close, Decimal(bar.volume)))  # fmt: skip
    findings += _corporate_action_findings(symbol, corporate_actions, kept, calendar,
                                           dividend_policy)  # fmt: skip
    suspects = frozenset(
        date.fromisoformat(f.detail[:10])
        for f in findings
        if f.issue is DataIssue.PRICE_DISCONTINUITY_UNEXPLAINED
    )
    ordered = tuple(sorted(set(findings)))
    if corporate_actions is None or any(f.issue in FATAL_ISSUES for f in ordered):
        return SeriesPreparation(symbol, None, ordered)
    split_tuple = tuple(sorted(corporate_actions.splits))
    dividends = (tuple(sorted(corporate_actions.dividends))
                 if dividend_policy is DividendPolicy.REQUIRE_COVERAGE else ())  # fmt: skip
    provenance = SeriesProvenance(
        symbol=symbol, feed=feed, adjustment="raw",
        first_session=kept[0].session_date if kept else None,
        last_session=kept[-1].session_date if kept else None,
        bar_count=len(kept),
        digest=_digest(symbol, feed, kept, corporate_actions, dividend_policy),
        dividend_policy=dividend_policy,
    )  # fmt: skip
    index = {b.session_date: i for i, b in enumerate(kept)}
    series = SymbolSeries(symbol, feed, tuple(kept), split_tuple, provenance, index, dividends,
                          suspects)  # fmt: skip
    return SeriesPreparation(symbol, series, ordered)


def _bar_identity(
    symbol: str, bar: DailyBar, feed: BarFeed, previous: date | None
) -> list[DataFinding]:
    out: list[DataFinding] = []
    if bar.symbol != symbol:
        out.append(DataFinding(DataIssue.WRONG_SYMBOL, bar.symbol))
    if bar.feed != feed:
        out.append(DataFinding(DataIssue.WRONG_FEED, bar.feed))
    if bar.adjustment != "raw":
        out.append(DataFinding(DataIssue.WRONG_ADJUSTMENT, bar.adjustment))
    if previous is not None:
        if bar.session_date == previous:
            out.append(DataFinding(DataIssue.DUPLICATE_SESSION, str(previous)))
        elif bar.session_date < previous:
            out.append(DataFinding(DataIssue.NOT_CHRONOLOGICAL,
                                   f"{bar.session_date} after {previous}"))  # fmt: skip
    return out


def _corporate_action_findings(
    symbol: str,
    ca: CorporateActions | None,
    bars: Sequence[StrategyBar],
    calendar: SessionCalendar,
    policy: DividendPolicy,
) -> list[DataFinding]:
    if ca is None:
        return [DataFinding(DataIssue.CORPORATE_ACTIONS_MISSING, "no corporate-action record")]
    out: list[DataFinding] = []
    if ca.symbol != symbol:
        out.append(DataFinding(DataIssue.CORPORATE_ACTIONS_SYMBOL_MISMATCH, ca.symbol))
    span = (bars[0].session_date, bars[-1].session_date) if bars else None
    out += _coverage_findings(ca.split_coverage, span, DataIssue.SPLIT_COVERAGE_MISSING,
                              DataIssue.SPLIT_COVERAGE_GAP)  # fmt: skip
    out += _split_findings(symbol, ca.splits, ca.split_coverage, calendar)
    if policy is DividendPolicy.REQUIRE_COVERAGE:
        out += _coverage_findings(ca.dividend_coverage, span,
                                  DataIssue.DIVIDEND_COVERAGE_MISSING,
                                  DataIssue.DIVIDEND_COVERAGE_GAP)  # fmt: skip
        out += _dividend_findings(symbol, ca, calendar)
    else:
        detail = f"price-return only; {len(ca.dividends)} event(s) ignored"
        out.append(DataFinding(DataIssue.DIVIDENDS_EXCLUDED, detail))
    out += _price_consistency(ca.splits, bars)
    return out


def _coverage_findings(
    window: CoverageWindow | None,
    span: tuple[date, date] | None,
    missing: DataIssue,
    gap: DataIssue,
) -> list[DataFinding]:
    if window is None:
        return [DataFinding(missing, "no attested coverage")]
    if span is not None and not (window.start <= span[0] and span[1] <= window.end):
        return [DataFinding(gap, f"covered {window.start}..{window.end}, "
                                 f"bars {span[0]}..{span[1]}")]  # fmt: skip
    return []


def _split_findings(
    symbol: str,
    splits: Sequence[SplitEvent],
    coverage: CoverageWindow | None,
    calendar: SessionCalendar,
) -> list[DataFinding]:
    out: list[DataFinding] = []
    seen: set[date] = set()
    for s in splits:
        if s.symbol != symbol:
            out.append(DataFinding(DataIssue.SPLIT_EVENT_INVALID, f"symbol {s.symbol}"))
        if s.effective_session in seen:
            out.append(DataFinding(DataIssue.SPLIT_EVENT_INVALID,
                                   f"two splits on {s.effective_session}"))  # fmt: skip
        seen.add(s.effective_session)
        if calendar.covers(s.effective_session) and not calendar.is_session(s.effective_session):
            out.append(DataFinding(DataIssue.SPLIT_EVENT_INVALID,
                                   f"{s.effective_session} is not a session"))  # fmt: skip
        if coverage is not None and not coverage.contains(s.effective_session):
            out.append(DataFinding(DataIssue.CORPORATE_ACTION_OUTSIDE_COVERAGE,
                                   f"split {s.effective_session}"))  # fmt: skip
    return out


def _dividend_findings(
    symbol: str, ca: CorporateActions, calendar: SessionCalendar
) -> list[DataFinding]:
    out: list[DataFinding] = []
    split_days = {s.effective_session for s in ca.splits}
    seen: set[date] = set()
    for d in ca.dividends:
        if d.symbol != symbol:
            out.append(DataFinding(DataIssue.DIVIDEND_EVENT_INVALID, f"symbol {d.symbol}"))
        if d.ex_session in seen:
            out.append(DataFinding(DataIssue.DIVIDEND_EVENT_INVALID,
                                   f"two dividends on {d.ex_session}"))  # fmt: skip
        seen.add(d.ex_session)
        if calendar.covers(d.ex_session) and not calendar.is_session(d.ex_session):
            out.append(DataFinding(DataIssue.DIVIDEND_EVENT_INVALID,
                                   f"{d.ex_session} is not a session"))  # fmt: skip
        if d.ex_session in split_days:
            out.append(DataFinding(DataIssue.CORPORATE_ACTION_CONFLICT,
                                   f"dividend and split both on {d.ex_session}"))  # fmt: skip
        if ca.dividend_coverage is not None and not ca.dividend_coverage.contains(d.ex_session):
            out.append(DataFinding(DataIssue.CORPORATE_ACTION_OUTSIDE_COVERAGE,
                                   f"dividend {d.ex_session}"))  # fmt: skip
    return out


def _price_consistency(
    splits: Sequence[SplitEvent], bars: Sequence[StrategyBar]
) -> list[DataFinding]:
    """Raw prices must agree with the split record. A recorded split of at least 3:2 that
    the overnight move does not show is fatal. An unrecorded overnight move of at least
    :data:`SUSPECT_SPLIT_MIN_MOVE` matching a plausible split ratio is fatal when the
    price *rose* (a possible unrecorded reverse split would fabricate a gain) and a
    non-fatal ``PRICE_DISCONTINUITY_UNEXPLAINED`` when it *fell* (excluding genuine
    crashes would be survivorship bias; a held position books it as a real gap, and no
    decision window may span it). Details start with the ISO session date."""
    out: list[DataFinding] = []
    by_day = {s.effective_session: s for s in splits}
    low, high = SPLIT_REFLECTION_BAND
    for prev, cur in itertools.pairwise(bars):
        move = DECIMAL_CONTEXT.divide(prev.close, cur.open)
        split = by_day.get(cur.session_date)
        if split is not None:
            ratio = split.ratio
            if ratio >= Fraction(3, 2) or ratio <= Fraction(2, 3):
                implied = DECIMAL_CONTEXT.divide(move * ratio.denominator, Decimal(ratio.numerator))
                if not low <= implied <= high:
                    detail = f"{cur.session_date}: close/open {move:.4f}"
                    out.append(DataFinding(DataIssue.SPLIT_NOT_REFLECTED_IN_PRICES, detail))
        elif abs(move - ONE) >= SUSPECT_SPLIT_MIN_MOVE and _snap_ratio(move) is not None:
            issue = (DataIssue.PRICE_DISCONTINUITY_UNEXPLAINED if move > ONE
                     else DataIssue.SPLIT_SUSPECTED_UNRECORDED)  # fmt: skip
            out.append(DataFinding(issue, f"{cur.session_date}: close/open {move:.4f}"))
    return out


def _digest(
    symbol: str,
    feed: str,
    bars: Sequence[StrategyBar],
    ca: CorporateActions,
    policy: DividendPolicy,
) -> str:
    h = hashlib.sha256()
    h.update(f"{symbol}|{feed}|raw|{policy.value}\n".encode())
    for b in bars:
        h.update(f"{b.session_date}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode())
    for s in sorted(ca.splits):
        h.update(f"split|{s.effective_session}|{s.new_shares}|{s.old_shares}\n".encode())
    for d in sorted(ca.dividends):
        h.update(f"div|{d.ex_session}|{d.pay_date}|{d.amount}\n".encode())
    for name, w in (("split_cov", ca.split_coverage), ("div_cov", ca.dividend_coverage)):
        h.update(f"{name}|{w.start if w else None}|{w.end if w else None}|"
                 f"{w.source if w else None}\n".encode())  # fmt: skip
    return h.hexdigest()


# ===================================================================== splits


@dataclass(frozen=True, slots=True)
class SplitDerivation:
    symbol: str
    events: tuple[SplitEvent, ...]
    findings: tuple[DataFinding, ...]
    coverage: CoverageWindow | None = None
    """The sessions the comparison attests (first..last common session), when resolved."""

    @property
    def resolved(self) -> bool:
        return not any(f.issue in FATAL_ISSUES for f in self.findings)


def derive_split_events(
    symbol: str, raw: Sequence[DailyBar], adjusted: Sequence[DailyBar]
) -> SplitDerivation:
    """Infer split events from a raw and a split-adjusted series of the same symbol, feed
    and sessions (both as fetched at one time).

    ``raw.close / adjusted.close`` is the cumulative factor of the splits after each
    session; a change between consecutive sessions is a split effective on the later one,
    with ratio = previous factor / current factor. The ratio is snapped to the nearest
    plausible split ratio (:func:`_plausible_ratios`) and accepted only within
    :data:`SPLIT_RATIO_TOLERANCE`; anything else (e.g. a large special dividend in an
    ``all``-adjusted series) is ``SPLIT_RATIO_UNRESOLVED`` and fails closed.
    """
    findings = _pair_findings(symbol, raw, adjusted)
    raw_by = {b.session_date: b for b in raw}
    adj_by = {b.session_date: b for b in adjusted}
    if findings:
        return SplitDerivation(symbol, (), tuple(sorted(set(findings))))
    events: list[SplitEvent] = []
    prev_factor: Decimal | None = None
    for d in sorted(raw_by):
        factor = DECIMAL_CONTEXT.divide(raw_by[d].close, adj_by[d].close)
        if prev_factor is not None:
            change = DECIMAL_CONTEXT.divide(prev_factor, factor)
            if abs(change - ONE) > SPLIT_RATIO_TOLERANCE:
                snapped = _snap_ratio(change)
                if snapped is None:
                    findings.append(DataFinding(DataIssue.SPLIT_RATIO_UNRESOLVED,
                                                f"{d}: factor change {change:.6f}"))  # fmt: skip
                else:
                    events.append(SplitEvent(symbol, d, snapped.numerator, snapped.denominator,
                                             source="derived:raw-vs-split"))  # fmt: skip
        prev_factor = factor
    ordered = tuple(sorted(set(findings)))
    days = sorted(raw_by)
    resolved = bool(days) and not any(f.issue in FATAL_ISSUES for f in ordered)
    coverage = CoverageWindow(days[0], days[-1], "derived:raw-vs-split") if resolved else None
    return SplitDerivation(symbol, tuple(events), ordered, coverage)


def _pair_findings(
    symbol: str, raw: Sequence[DailyBar], adjusted: Sequence[DailyBar]
) -> list[DataFinding]:
    out: list[DataFinding] = []
    for label, bars, expected in (("raw", raw, "raw"), ("adjusted", adjusted, "split")):
        for bar in bars:
            if bar.adjustment != expected:
                out.append(DataFinding(DataIssue.WRONG_ADJUSTMENT, f"{label} {bar.adjustment}"))
            if bar.symbol != symbol:
                out.append(DataFinding(DataIssue.WRONG_SYMBOL, bar.symbol))
    raw_days = [b.session_date for b in raw]
    adj_days = [b.session_date for b in adjusted]
    if (
        set(raw_days) != set(adj_days)
        or len(set(raw_days)) != len(raw_days)
        or len(set(adj_days)) != len(adj_days)
    ):
        detail = f"raw {len(raw)} bars, adjusted {len(adjusted)} bars"
        out.append(DataFinding(DataIssue.ADJUSTED_COVERAGE_MISMATCH, detail))
    return out


def _plausible_ratios() -> tuple[Fraction, ...]:
    """Split ratios with one term <= :data:`MAX_SPLIT_MINOR_TERM` and the other <=
    :data:`MAX_SPLIT_TERM` (2:1, 3:2, 5:4, 1:10, 1:20 ...), excluding 1."""
    out = set()
    for minor in range(1, MAX_SPLIT_MINOR_TERM + 1):
        for major in range(1, MAX_SPLIT_TERM + 1):
            out.add(Fraction(major, minor))
            out.add(Fraction(minor, major))
    out.discard(Fraction(1))
    return tuple(sorted(out))


_RATIOS: Final = _plausible_ratios()


def _snap_ratio(change: Decimal) -> Fraction | None:
    """The plausible split ratio nearest ``change`` within the tolerance, else None."""
    best: Fraction | None = None
    best_err: Decimal | None = None
    for candidate in _RATIOS:
        exact = DECIMAL_CONTEXT.divide(Decimal(candidate.numerator), Decimal(candidate.denominator))
        err = abs(DECIMAL_CONTEXT.divide(change, exact) - ONE)
        if best_err is None or err < best_err:
            best, best_err = candidate, err
    if best_err is None or best_err > SPLIT_RATIO_TOLERANCE:
        return None
    return best


__all__ = [
    "FATAL_ISSUES",
    "MAX_SPLIT_MINOR_TERM",
    "MAX_SPLIT_TERM",
    "SPLIT_RATIO_TOLERANCE",
    "BarWindow",
    "CashDividend",
    "CorporateActions",
    "CoverageWindow",
    "DataContractError",
    "DataFinding",
    "DataIssue",
    "DividendPolicy",
    "PriceBasis",
    "SeriesPreparation",
    "SeriesProvenance",
    "SessionCalendar",
    "SplitDerivation",
    "SplitEvent",
    "StrategyBar",
    "SymbolSeries",
    "WindowIssue",
    "WindowResult",
    "WindowStatus",
    "derive_split_events",
    "prepare_series",
]
