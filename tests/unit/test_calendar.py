"""Step 5: trading/settlement calendar logic against the deterministic offline fixture."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.calendar import (
    ALPACA_DOCUMENTED_CALENDAR_END,
    MARKET_TZ,
    CalendarCoverageError,
    CalendarData,
    CalendarFetch,
    CalendarKind,
    ContradictoryCalendarError,
    DateRange,
    NotATradingSessionError,
    SettlementDateUnavailableError,
    StaleCalendarError,
    TradingCalendar,
    TradingSession,
)
from tests.support.calendar_fixture import (
    BASELINE_ASSUMPTION,
    FIXTURE_END,
    FIXTURE_START,
    NON_SETTLEMENT_WEEKDAYS,
    fixture_sessions,
    is_fixture_settlement_day,
    settlement_fetch,
    trading_fetch,
)

AS_OF = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
FETCHED = AS_OF - timedelta(hours=1)
MAX_AGE = timedelta(hours=36)
D = date


def build(*fetches: CalendarFetch) -> CalendarData:
    data = CalendarData()
    for f in fetches:
        data = data.with_fetch(f)
    return data


def cal(*fetches: CalendarFetch, as_of: datetime = AS_OF) -> TradingCalendar:
    return TradingCalendar(build(*fetches), as_of=as_of, max_age=MAX_AGE)


FULL = cal(trading_fetch(FETCHED), settlement_fetch(FETCHED))
DERIVED_ONLY = cal(trading_fetch(FETCHED, with_settlement_dates=False), settlement_fetch(FETCHED))
EXPLICIT_ONLY = cal(trading_fetch(FETCHED))
SESSIONS = [s.session_date for s in fixture_sessions()]


# ---------------------------------------------------------------- settlement


@pytest.mark.parametrize(
    "calendar", [FULL, DERIVED_ONLY, EXPLICIT_ONLY], ids=["both", "derived", "explicit"]
)
@pytest.mark.parametrize(
    ("trade", "settles", "case"),
    [
        (D(2026, 11, 9), D(2026, 11, 10), "ordinary Monday T+1"),
        (D(2026, 11, 12), D(2026, 11, 13), "ordinary Thursday T+1"),
        (D(2026, 11, 6), D(2026, 11, 9), "Friday -> Monday over weekend"),
        (D(2026, 11, 10), D(2026, 11, 12), "Veterans Day: bank closed, market open"),
        (D(2026, 11, 11), D(2026, 11, 12), "trade on Veterans Day itself"),
        (D(2026, 10, 9), D(2026, 10, 13), "Friday before Columbus Day"),
        (D(2026, 10, 12), D(2026, 10, 13), "trade on Columbus Day"),
        (D(2026, 11, 25), D(2026, 11, 27), "before Thanksgiving"),
        (D(2026, 11, 27), D(2026, 11, 30), "early close Friday"),
        (D(2026, 12, 24), D(2026, 12, 28), "early close before Christmas"),
        (D(2026, 12, 31), D(2027, 1, 4), "year end over New Year holiday"),
        (D(2026, 4, 2), D(2026, 4, 6), "Good Friday: no CNS settlement (VERIFIED A#9732)"),
        (D(2026, 7, 2), D(2026, 7, 6), "Jul 3: no CNS settlement (VERIFIED A#9778)"),
        (D(2026, 6, 18), D(2026, 6, 22), "Juneteenth: DTC closed (VERIFIED B#24302-26)"),
        (D(2027, 7, 2), D(2027, 7, 6), "Jul 5 2027 (ASSUMPTION)"),
        (D(2027, 12, 23), D(2027, 12, 27), "Christmas observed 2027 (ASSUMPTION)"),
    ],
)
def test_settlement_dates(calendar: TradingCalendar, trade: date, settles: date, case: str) -> None:
    assert calendar.settlement_date(trade) == settles, case


@pytest.mark.parametrize(
    "d", [D(2026, 11, 7), D(2026, 11, 8), D(2026, 11, 26), D(2026, 4, 3), D(2027, 1, 1)]
)
def test_settlement_requires_a_trading_session(d: date) -> None:
    with pytest.raises(NotATradingSessionError):
        FULL.settlement_date(d)


def test_never_guesses_without_authoritative_data() -> None:
    """No per-session settlement date and no settlement calendar: refuse, don't compute T+1."""
    calendar = cal(trading_fetch(FETCHED, with_settlement_dates=False))
    assert calendar.is_trading_session(D(2026, 11, 9))
    for d in SESSIONS[:50]:
        with pytest.raises(SettlementDateUnavailableError):
            calendar.settlement_date(d)


def test_settlement_unavailable_when_settlement_coverage_ends() -> None:
    calendar = cal(
        trading_fetch(FETCHED, with_settlement_dates=False),
        settlement_fetch(FETCHED, end=D(2026, 11, 13)),
    )
    assert calendar.settlement_date(D(2026, 11, 12)) == D(2026, 11, 13)
    with pytest.raises(SettlementDateUnavailableError):
        calendar.settlement_date(D(2026, 11, 13))


def test_conflicting_settlement_sources_rejected() -> None:
    sessions = list(fixture_sessions(with_settlement_dates=True))
    idx = next(i for i, s in enumerate(sessions) if s.session_date == D(2026, 11, 10))
    sessions[idx] = replace(sessions[idx], settlement_date=D(2026, 11, 11))  # a bank holiday
    bad = replace(trading_fetch(FETCHED), sessions=tuple(sessions))
    with pytest.raises(ContradictoryCalendarError, match="2026-11-10"):
        cal(bad, settlement_fetch(FETCHED))


def test_explicit_settlement_disagreeing_with_derived_rejected() -> None:
    sessions = list(fixture_sessions(with_settlement_dates=True))
    idx = next(i for i, s in enumerate(sessions) if s.session_date == D(2026, 11, 9))
    sessions[idx] = replace(sessions[idx], settlement_date=D(2026, 11, 12))  # T+3 not T+1
    data = build(
        replace(trading_fetch(FETCHED), sessions=tuple(sessions)), settlement_fetch(FETCHED)
    )
    assert any("disagrees" in p for p in data.audit())
    with pytest.raises(ContradictoryCalendarError):
        TradingCalendar(data, as_of=AS_OF, max_age=MAX_AGE)


def test_exchange_closure_that_is_a_settlement_day_is_supported() -> None:
    """Logic test with a hand-built hypothetical calendar (not a real-world claim): if the
    authoritative settlement calendar lists a day on which the exchange is closed, a trade
    on the prior session settles that day."""
    start, end = D(2030, 6, 3), D(2030, 6, 14)  # outside the fixture: purely hypothetical
    fetched = datetime(2030, 6, 1, tzinfo=UTC)
    sessions = tuple(
        s for s in _sessions_for(start, end) if s.session_date != D(2030, 6, 7)
    )  # exchange closed Fri Jun 7
    trading = CalendarFetch(CalendarKind.TRADING, start, end, fetched, "hypothetical", sessions)
    days = tuple(
        start + timedelta(days=i) for i in range(12) if (start + timedelta(days=i)).weekday() < 5
    )
    settlement = CalendarFetch(
        CalendarKind.SETTLEMENT, start, end, fetched, "hypothetical", settlement_days=days
    )
    calendar = cal(trading, settlement, as_of=fetched)
    assert not calendar.is_trading_session(D(2030, 6, 7))
    assert calendar.is_settlement_day(D(2030, 6, 7))
    assert calendar.settlement_date(D(2030, 6, 6)) == D(2030, 6, 7)


def _sessions_for(start: date, end: date) -> list[TradingSession]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(_session(d))
        d += timedelta(days=1)
    return out


def test_fixture_provenance_is_explicit() -> None:
    verified = {d for d, p in NON_SETTLEMENT_WEEKDAYS.items() if p.status == "VERIFIED"}
    assert verified == {D(2026, 4, 3), D(2026, 6, 19), D(2026, 7, 3)}
    for d, p in NON_SETTLEMENT_WEEKDAYS.items():
        assert p.source, d
        if p.status == "ASSUMPTION":
            assert "step 15" in p.source
    assert NON_SETTLEMENT_WEEKDAYS[D(2026, 11, 26)].status == "ASSUMPTION"  # Thanksgiving
    assert BASELINE_ASSUMPTION.status == "ASSUMPTION"


def test_settlement_day_queries() -> None:
    assert not FULL.is_settlement_day(D(2026, 4, 3))  # Good Friday: VERIFIED no CNS settlement
    assert FULL.is_settlement_day(D(2026, 4, 6))
    assert not FULL.is_settlement_day(D(2026, 11, 11))  # Veterans Day
    assert not FULL.is_settlement_day(D(2026, 11, 14))  # Saturday
    with pytest.raises(CalendarCoverageError):
        EXPLICIT_ONLY.is_settlement_day(D(2026, 11, 10))


# ------------------------------------------------------------------ sessions


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (D(2026, 11, 9), True),
        (D(2026, 11, 7), False),  # Saturday
        (D(2026, 11, 11), True),  # Veterans Day: market open
        (D(2026, 10, 12), True),  # Columbus Day: market open
        (D(2026, 11, 26), False),  # Thanksgiving
        (D(2026, 4, 3), False),  # Good Friday
        (D(2026, 12, 31), True),
        (D(2027, 1, 1), False),
    ],
)
def test_is_trading_session(d: date, expected: bool) -> None:
    assert FULL.is_trading_session(d) is expected


@pytest.mark.parametrize(
    ("d", "nxt", "prev"),
    [
        (D(2026, 11, 6), D(2026, 11, 9), D(2026, 11, 5)),
        (D(2026, 11, 7), D(2026, 11, 9), D(2026, 11, 6)),  # from a Saturday
        (D(2026, 11, 25), D(2026, 11, 27), D(2026, 11, 24)),
        (D(2026, 11, 26), D(2026, 11, 27), D(2026, 11, 25)),  # from a holiday
        (D(2026, 12, 31), D(2027, 1, 4), D(2026, 12, 30)),
        (D(2027, 1, 4), D(2027, 1, 5), D(2026, 12, 31)),  # back across year end
        (D(2026, 4, 2), D(2026, 4, 6), D(2026, 4, 1)),
    ],
)
def test_next_and_previous_sessions(d: date, nxt: date, prev: date) -> None:
    assert FULL.next_trading_session(d) == nxt
    assert FULL.previous_trading_session(d) == prev


def test_early_close_sessions() -> None:
    for d in (D(2026, 11, 27), D(2026, 12, 24), D(2027, 11, 26)):
        s = FULL.session(d)
        assert s.is_early_close
        assert s.close_local.time() == time(13, 0)
        assert s.open_local.time() == time(9, 30)
    regular = FULL.session(D(2026, 11, 25))
    assert not regular.is_early_close
    assert regular.close_local.time() == time(16, 0)
    assert regular.close_at.tzinfo is UTC


def test_dst_transition_sessions_have_correct_utc_times() -> None:
    before = FULL.session(D(2026, 10, 30))  # EDT, UTC-4
    after = FULL.session(D(2026, 11, 2))  # EST, UTC-5
    assert before.open_at.time() == time(13, 30)
    assert after.open_at.time() == time(14, 30)


def test_session_on_non_session_raises() -> None:
    with pytest.raises(NotATradingSessionError):
        FULL.session(D(2026, 11, 26))


# ------------------------------------------------------------------ coverage


@pytest.mark.parametrize("d", [D(1969, 12, 31), D(2025, 11, 30), D(2028, 1, 1), D(2030, 1, 1)])
def test_out_of_range_dates_raise(d: date) -> None:
    with pytest.raises(CalendarCoverageError):
        FULL.is_trading_session(d)


def test_navigation_stops_at_coverage_edge() -> None:
    calendar = cal(trading_fetch(FETCHED, end=D(2026, 11, 13)), settlement_fetch(FETCHED))
    assert calendar.next_trading_session(D(2026, 11, 12)) == D(2026, 11, 13)
    with pytest.raises(CalendarCoverageError):
        calendar.next_trading_session(D(2026, 11, 13))
    with pytest.raises(CalendarCoverageError):
        FULL.previous_trading_session(FIXTURE_START)


def test_coverage_report_lists_missing_ranges() -> None:
    calendar = cal(
        trading_fetch(FETCHED, start=D(2026, 11, 1), end=D(2026, 11, 10)),
        trading_fetch(FETCHED, start=D(2026, 11, 16), end=D(2026, 11, 30)),
        settlement_fetch(FETCHED, start=D(2026, 11, 1), end=D(2026, 11, 30)),
    )
    report = calendar.calendar_coverage(D(2026, 11, 1), D(2026, 11, 30))
    assert not report.complete
    assert report.missing[CalendarKind.TRADING] == (DateRange(D(2026, 11, 11), D(2026, 11, 15)),)
    assert report.missing[CalendarKind.SETTLEMENT] == ()
    assert report.describe() == ["TRADING missing: 2026-11-11..2026-11-15"]
    with pytest.raises(CalendarCoverageError, match="TRADING missing"):
        calendar.require_coverage(D(2026, 11, 1), D(2026, 11, 30))
    calendar.require_coverage(D(2026, 11, 1), D(2026, 11, 10))
    assert FULL.calendar_coverage(FIXTURE_START, FIXTURE_END).complete


def test_future_coverage_requirement() -> None:
    FULL.require_future_coverage(10)
    short = cal(
        trading_fetch(FETCHED, end=D(2026, 11, 15)), settlement_fetch(FETCHED, end=D(2026, 11, 30))
    )
    with pytest.raises(CalendarCoverageError, match="TRADING missing: 2026-11-16"):
        short.require_future_coverage(10)
    with pytest.raises(ValueError, match="positive"):
        FULL.require_future_coverage(0)


def test_stale_data_rejected_for_future_dates_only() -> None:
    later = FETCHED + MAX_AGE + timedelta(hours=1)
    calendar = cal(trading_fetch(FETCHED), settlement_fetch(FETCHED), as_of=later)
    # Dates already past when fetched are history and stay usable ...
    assert calendar.settlement_date(D(2026, 11, 5)) == D(2026, 11, 6)
    # ... but a historical trade whose settlement date was not yet past is not.
    with pytest.raises(StaleCalendarError, match="2026-11-09"):
        calendar.settlement_date(D(2026, 11, 6))
    with pytest.raises(StaleCalendarError):
        calendar.is_trading_session(D(2026, 11, 20))
    with pytest.raises(StaleCalendarError):
        calendar.settlement_date(D(2026, 11, 20))
    report = calendar.calendar_coverage(D(2026, 11, 1), D(2026, 11, 30))
    assert report.stale[CalendarKind.TRADING] == (DateRange(D(2026, 11, 9), D(2026, 11, 30)),)
    # A newer fetch over the same dates makes them fresh again.
    refreshed = cal(
        trading_fetch(FETCHED), settlement_fetch(FETCHED),
        trading_fetch(later), settlement_fetch(later), as_of=later,
    )  # fmt: skip
    assert refreshed.is_trading_session(D(2026, 11, 20))


def test_stale_settlement_calendar_blocks_explicit_dates() -> None:
    later = FETCHED + MAX_AGE + timedelta(hours=1)
    calendar = cal(trading_fetch(later), settlement_fetch(FETCHED), as_of=later)
    with pytest.raises(StaleCalendarError):
        calendar.settlement_date(D(2026, 11, 20))


def test_horizon_status() -> None:
    status = FULL.horizon_status(warning_days=365)
    assert status.trading_horizon == FIXTURE_END
    assert status.settlement_horizon == FIXTURE_END
    assert status.days_remaining == (FIXTURE_END - D(2026, 11, 9)).days
    assert status.provider_documented_end == ALPACA_DOCUMENTED_CALENDAR_END
    assert not status.warning
    assert FULL.horizon_status(warning_days=500).warning
    short = cal(trading_fetch(FETCHED, end=D(2026, 12, 31)))
    s = short.horizon_status(warning_days=365)
    assert s.settlement_horizon is None
    assert s.days_remaining is None
    assert s.warning


# ------------------------------------------------------------- invalid input


@pytest.mark.parametrize("bad", [datetime(2026, 11, 9, tzinfo=UTC), "2026-11-09", 20261109, None])
def test_invalid_date_arguments(bad: object) -> None:
    for fn in (
        FULL.is_trading_session,
        FULL.next_trading_session,
        FULL.previous_trading_session,
        FULL.settlement_date,
        FULL.is_settlement_day,
    ):
        with pytest.raises(TypeError):
            fn(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        FULL.calendar_coverage(bad, D(2026, 11, 9))  # type: ignore[arg-type]


def test_invalid_constructor_and_range_arguments() -> None:
    with pytest.raises(ValueError, match="start"):
        FULL.calendar_coverage(D(2026, 11, 10), D(2026, 11, 9))
    with pytest.raises(ValueError, match="timezone-aware"):
        TradingCalendar(CalendarData(), as_of=datetime(2026, 11, 9), max_age=MAX_AGE)  # noqa: DTZ001
    with pytest.raises(ValueError, match="positive"):
        TradingCalendar(CalendarData(), as_of=AS_OF, max_age=timedelta(0))
    with pytest.raises(ValueError, match="timezone-aware"):
        TradingSession(D(2026, 11, 9), datetime(2026, 11, 9, 14, 30), AS_OF)  # noqa: DTZ001


# ------------------------------------------------------------ fetch validation


def _session(d: date, open_t: time = time(9, 30), close_t: time = time(16, 0),
             settles: date | None = None) -> TradingSession:  # fmt: skip
    return TradingSession(
        d,
        datetime.combine(d, open_t, tzinfo=MARKET_TZ),
        datetime.combine(d, close_t, tzinfo=MARKET_TZ),
        settles,
    )


@pytest.mark.parametrize(
    ("sessions", "message"),
    [
        ((_session(D(2026, 11, 7)),), "weekend"),
        ((_session(D(2026, 11, 9)), _session(D(2026, 11, 9))), "duplicate session"),
        ((_session(D(2026, 12, 1)),), "outside requested range"),
        ((_session(D(2026, 11, 9), time(16, 0), time(9, 30)),), "open is not before close"),
        ((_session(D(2026, 11, 9), time(9, 30), time(17, 0)),), "beyond regular hours"),
        ((_session(D(2026, 11, 9), time(4, 0), time(16, 0)),), "beyond regular hours"),
        ((_session(D(2026, 11, 9), settles=D(2026, 11, 9)),), "not after the trade date"),
        ((_session(D(2026, 11, 9), settles=D(2026, 11, 14)),), "is a weekend"),
        ((_session(D(2026, 11, 9), settles=D(2026, 12, 9)),), "implausibly late"),
    ],
)
def test_invalid_trading_fetch(sessions: tuple[TradingSession, ...], message: str) -> None:
    fetch = CalendarFetch(
        CalendarKind.TRADING, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "test", sessions
    )
    assert any(message in p for p in fetch.problems()), fetch.problems()


def test_session_crossing_midnight_detected() -> None:
    s = TradingSession(
        D(2026, 11, 9),
        datetime(2026, 11, 9, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 10, 5, 0, tzinfo=UTC),  # 00:00 ET next day
    )
    assert any("not on the session date" in p for p in s.problems())


@pytest.mark.parametrize(
    ("fetch", "message"),
    [
        (CalendarFetch(CalendarKind.SETTLEMENT, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t",
                       settlement_days=(D(2026, 11, 7),)), "weekend"),
        (CalendarFetch(CalendarKind.SETTLEMENT, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t",
                       settlement_days=(D(2026, 11, 9), D(2026, 11, 9))), "duplicate"),
        (CalendarFetch(CalendarKind.SETTLEMENT, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t",
                       settlement_days=(D(2026, 12, 1),)), "outside requested range"),
        (CalendarFetch(CalendarKind.SETTLEMENT, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t"),
         "no settlement days"),
        (CalendarFetch(CalendarKind.TRADING, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t"),
         "no sessions"),
        (CalendarFetch(CalendarKind.TRADING, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t",
                       settlement_days=(D(2026, 11, 9),)), "must not carry settlement days"),
        (CalendarFetch(CalendarKind.SETTLEMENT, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "t",
                       sessions=(_session(D(2026, 11, 9)),)), "must not carry sessions"),
        (CalendarFetch(CalendarKind.TRADING, D(2026, 11, 30), D(2026, 11, 1), FETCHED, " "),
         "blank"),
        (CalendarFetch(CalendarKind.TRADING, D(2026, 11, 30), D(2026, 11, 1), FETCHED, "t"),
         "after end"),
    ],
)  # fmt: skip
def test_invalid_fetch_shapes(fetch: CalendarFetch, message: str) -> None:
    assert any(message in p for p in fetch.problems()), fetch.problems()


def test_short_empty_fetch_is_allowed() -> None:
    """A range of only weekend/holiday days legitimately has no sessions."""
    fetch = CalendarFetch(CalendarKind.TRADING, D(2026, 11, 7), D(2026, 11, 8), FETCHED, "t")
    assert fetch.problems() == []
    assert fetch.row_count == 0


def test_fetch_requires_aware_timestamp_and_real_dates() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CalendarFetch(CalendarKind.TRADING, D(2026, 11, 1), D(2026, 11, 2),
                      datetime(2026, 11, 9), "t")  # noqa: DTZ001  # fmt: skip
    with pytest.raises(TypeError):
        CalendarFetch(CalendarKind.TRADING, "2026-11-01", D(2026, 11, 2), FETCHED, "t")  # type: ignore[arg-type]


def test_payload_hash_is_content_sensitive_and_stable() -> None:
    a = trading_fetch(FETCHED, end=D(2026, 1, 31))
    b = trading_fetch(FETCHED + timedelta(hours=5), end=D(2026, 1, 31))
    assert a.payload_sha256 == b.payload_sha256  # fetch time is not content
    c = trading_fetch(FETCHED, end=D(2026, 1, 30))
    assert a.payload_sha256 != c.payload_sha256


# ------------------------------------------------------------------ conflicts


def test_conflicts_with_previously_accepted_history() -> None:
    base = build(trading_fetch(FETCHED, end=D(2026, 11, 30)), settlement_fetch(FETCHED))
    sessions = list(fixture_sessions(end=D(2026, 11, 30)))
    removed = [s for s in sessions if s.session_date != D(2026, 11, 16)]  # e.g. day of mourning
    assert any("disappeared" in p for p in base.conflicts_with(
        replace(trading_fetch(FETCHED, end=D(2026, 11, 30)), sessions=tuple(removed))))  # fmt: skip
    added = [*sessions, _session(D(2026, 11, 26))]  # Thanksgiving suddenly open
    assert any("appeared" in p for p in base.conflicts_with(
        replace(trading_fetch(FETCHED, end=D(2026, 11, 30)), sessions=tuple(added))))  # fmt: skip
    changed = [_session(s.session_date, close_t=time(13, 0), settles=s.settlement_date)
               if s.session_date == D(2026, 11, 20) else s for s in sessions]  # fmt: skip
    assert any("hours changed" in p for p in base.conflicts_with(
        replace(trading_fetch(FETCHED, end=D(2026, 11, 30)), sessions=tuple(changed))))  # fmt: skip
    withdrawn = [replace(s, settlement_date=None) for s in sessions]
    nov = trading_fetch(FETCHED, end=D(2026, 11, 30))
    assert any(
        "withdrawn" in p for p in base.conflicts_with(replace(nov, sessions=tuple(withdrawn)))
    )
    days = [d for d in settlement_fetch(FETCHED).settlement_days if d != D(2026, 11, 20)]
    assert any("settlement-day status" in p for p in base.conflicts_with(
        replace(settlement_fetch(FETCHED), settlement_days=tuple(days))))  # fmt: skip
    # Identical refetch and refetch outside prior coverage are not conflicts.
    assert base.conflicts_with(trading_fetch(FETCHED, end=D(2026, 11, 30))) == []
    assert base.conflicts_with(trading_fetch(FETCHED, start=D(2026, 12, 1))) == []


def test_filling_in_settlement_dates_is_not_a_conflict() -> None:
    base = build(trading_fetch(FETCHED, with_settlement_dates=False))
    assert base.conflicts_with(trading_fetch(FETCHED)) == []
    merged = base.with_fetch(trading_fetch(FETCHED, with_settlement_dates=False))
    assert merged.sessions[D(2026, 11, 9)].settlement_date is None


# --------------------------------------------------------- property-based tests

session_dates = st.sampled_from(SESSIONS[:-5])
any_dates = st.dates(
    min_value=FIXTURE_START + timedelta(days=10), max_value=FIXTURE_END - timedelta(days=10)
)


@given(session_dates)
def test_settlement_is_next_settlement_day(d: date) -> None:
    s = FULL.settlement_date(d)
    assert s > d
    assert s.weekday() < 5
    assert FULL.is_settlement_day(s)
    assert is_fixture_settlement_day(s)
    between = [d + timedelta(days=i) for i in range(1, (s - d).days)]
    assert not any(FULL.is_settlement_day(x) for x in between)
    assert (s - d).days <= 5
    assert DERIVED_ONLY.settlement_date(d) == s == EXPLICIT_ONLY.settlement_date(d)


@given(session_dates, session_dates)
def test_settlement_is_monotonic(a: date, b: date) -> None:
    if a <= b:
        assert FULL.settlement_date(a) <= FULL.settlement_date(b)


@given(any_dates)
def test_navigation_ordering(d: date) -> None:
    nxt = FULL.next_trading_session(d)
    prev = FULL.previous_trading_session(d)
    assert prev < d < nxt
    assert FULL.is_trading_session(nxt)
    assert FULL.is_trading_session(prev)
    assert not any(
        FULL.is_trading_session(prev + timedelta(days=i)) for i in range(1, (d - prev).days)
    )
    assert not any(FULL.is_trading_session(d + timedelta(days=i)) for i in range(1, (nxt - d).days))
    if FULL.is_trading_session(d):
        assert FULL.previous_trading_session(nxt) == d
        assert FULL.next_trading_session(prev) == d
    assert nxt.weekday() < 5


@given(any_dates)
def test_settlement_days_and_sessions_partition_correctly(d: date) -> None:
    if d.weekday() >= 5:
        assert not FULL.is_trading_session(d)
        assert not FULL.is_settlement_day(d)
    if FULL.is_trading_session(d):
        assert FULL.settlement_date(d) > d
