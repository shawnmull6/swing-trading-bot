"""Historical data contract for the strategy (Step 16.14, Phase A)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from swing_trading_bot.broker.mapping import daily_bar_from_raw
from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.strategy.market_data import (
    BarWindow,
    CashDividend,
    CorporateActions,
    CoverageWindow,
    DataContractError,
    DataIssue,
    DividendPolicy,
    PriceBasis,
    SeriesPreparation,
    SessionCalendar,
    SplitEvent,
    StrategyBar,
    WindowIssue,
    WindowStatus,
    derive_split_events,
    prepare_series,
)
from tests.support import bars_fixture
from tests.support.calendar_fixture import fixture_sessions
from tests.support.strategy_fixture import (
    CALENDAR,
    SESSIONS,
    after,
    attested,
    bar,
    candle,
    prepared,
    received,
    setup_scenario,
    t_of,
)

D = Decimal


def _series(n: int = 30, symbol: str = "ABC", start: date = date(2026, 3, 2)) -> list[DailyBar]:
    i = SESSIONS.index(start)
    return [bar(symbol, d, candle(D(50) + k, D(51) + k)) for k, d in enumerate(SESSIONS[i : i + n])]


# --------------------------------------------------------------------- calendar
def test_calendar_requires_strictly_increasing_sessions() -> None:
    sessions = fixture_sessions(date(2026, 3, 2), date(2026, 3, 6))
    with pytest.raises(DataContractError):
        SessionCalendar([sessions[1], sessions[0]])
    with pytest.raises(DataContractError):
        SessionCalendar([])
    cal = SessionCalendar(sessions)
    assert cal.next_session(date(2026, 3, 6)) is None
    assert cal.window(date(2026, 3, 4), 4) is None  # not enough history in the calendar
    assert cal.window(date(2026, 3, 4), 3) == (date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4))
    with pytest.raises(DataContractError):
        cal.index(date(2026, 3, 7))  # Saturday


def test_settlement_date_comes_from_the_calendar() -> None:
    # Friday 2026-10-09 settles Tuesday 2026-10-13 (Columbus Day: market open, no settlement)
    assert CALENDAR.settlement_date(date(2026, 10, 9)) == date(2026, 10, 13)


# --------------------------------------------------------------------- series
def test_valid_series_is_accepted_with_provenance() -> None:
    prep = prepare_series("ABC", _series(), calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert prep.accepted and prep.findings == ()
    assert prep.series is not None
    prov = prep.series.provenance
    assert (prov.feed, prov.adjustment, prov.bar_count) == ("sip", "raw", 30)
    assert prov.first_session == date(2026, 3, 2)
    assert len(prov.digest) == 64
    # same input -> same digest; any change -> different digest
    again = prepare_series("ABC", _series(), calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert again.series is not None and again.series.provenance.digest == prov.digest
    changed = _series()
    changed[3] = replace(changed[3], close=changed[3].close + D("0.01"))
    other = prepare_series("ABC", changed, calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert other.series is not None and other.series.provenance.digest != prov.digest


@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (lambda b: [*b[:5], replace(b[5], symbol="XYZ"), *b[6:]], DataIssue.WRONG_SYMBOL),
        (lambda b: [*b[:5], replace(b[5], feed="iex"), *b[6:]], DataIssue.WRONG_FEED),
        (lambda b: [*b[:5], replace(b[5], adjustment="split"), *b[6:]], DataIssue.WRONG_ADJUSTMENT),
        (lambda b: [*b[:5], b[4], *b[6:]], DataIssue.DUPLICATE_SESSION),
        (lambda b: [*b[:4], b[5], b[4], *b[6:]], DataIssue.NOT_CHRONOLOGICAL),
    ],
)
def test_series_faults_reject_the_whole_series(mutate, issue) -> None:  # type: ignore[no-untyped-def]
    prep = prepare_series(
        "ABC", mutate(_series()), calendar=CALENDAR, corporate_actions=attested("ABC")
    )
    assert not prep.accepted
    assert issue in {f.issue for f in prep.findings}


def test_bar_on_a_non_session_rejects_the_series() -> None:
    saturday = date(2026, 3, 7)
    rogue = DailyBar(symbol="ABC", session_date=saturday, open=D(50), high=D(51), low=D(49),
                     close=D(50), volume=1, vwap=None, trade_count=None, feed="sip",
                     adjustment="raw", as_of=t_of(saturday),
                     received_at=received(saturday))  # fmt: skip
    bars = _series(5)
    prep = prepare_series(
        "ABC", [*bars[:5], rogue], calendar=CALENDAR, corporate_actions=attested("ABC")
    )
    assert not prep.accepted
    assert DataIssue.NOT_A_TRADING_SESSION in {f.issue for f in prep.findings}


def test_incomplete_bar_is_excluded_and_counts_as_missing() -> None:
    bars = _series(30)
    last = bars[-1]
    provisional = replace(last, received_at=after(last.session_date, hours=-3))  # 13:00 ET
    prep = prepare_series(
        "ABC", [*bars[:-1], provisional], calendar=CALENDAR, corporate_actions=attested("ABC")
    )
    assert prep.accepted and prep.series is not None
    assert [f.issue for f in prep.findings] == [DataIssue.INCOMPLETE_BAR]
    assert prep.series.bar(last.session_date) is None
    res = prep.series.window(CALENDAR, last.session_date, 10)
    assert res.status is WindowStatus.INSUFFICIENT
    assert res.issue is WindowIssue.DECISION_SESSION_MISSING


def test_bar_outside_the_calendar_is_excluded() -> None:
    short = SessionCalendar(fixture_sessions(date(2026, 3, 2), date(2026, 3, 31)))
    bars = _series(30)  # runs into April
    prep = prepare_series("ABC", bars, calendar=short, corporate_actions=attested("ABC"))
    assert prep.accepted and prep.series is not None
    assert DataIssue.OUTSIDE_CALENDAR in {f.issue for f in prep.findings}
    assert prep.series.provenance.last_session == date(2026, 3, 31)


def test_invalid_split_event_rejects_the_series() -> None:
    bad = SplitEvent("OTHER", date(2026, 3, 10), 2, 1)
    prep = prepare_series("ABC", _series(), calendar=CALENDAR,
                          corporate_actions=attested("ABC", [bad]))  # fmt: skip
    assert not prep.accepted
    with pytest.raises(DataContractError):
        SplitEvent("ABC", date(2026, 3, 10), 2, 2)
    with pytest.raises(DataContractError):
        SplitEvent("ABC", date(2026, 3, 10), 0, 1)


def test_strategy_bar_validation() -> None:
    with pytest.raises(DataContractError):
        StrategyBar("A", date(2026, 3, 2), D(10), D(9), D(8), D(9), D(1))  # open > high
    with pytest.raises(DataContractError):
        StrategyBar("A", date(2026, 3, 2), D(10), D(11), D(9), D(10), D(-1))
    with pytest.raises(DataContractError):
        StrategyBar("A", date(2026, 3, 2), 10.0, D(11), D(9), D(10), D(1))  # type: ignore[arg-type]


# --------------------------------------------------------------------- windows
def test_window_contains_only_sessions_up_to_the_decision() -> None:
    prep = prepare_series("ABC", _series(30), calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert prep.series is not None
    decision = SESSIONS[SESSIONS.index(date(2026, 3, 2)) + 19]
    res = prep.series.window(CALENDAR, decision, 10)
    assert res.window is not None
    assert res.window.bars[-1].session_date == decision
    assert all(b.session_date <= decision for b in res.window.bars)
    assert len(res.window.bars) == 10
    assert res.window.decision_at == CALENDAR.session(decision).close_at
    assert res.window.basis is PriceBasis.RAW


def test_window_refuses_future_bars_by_construction() -> None:
    bars = [StrategyBar("A", d, D(10), D(11), D(9), D(10), D(1)) for d in SESSIONS[:3]]
    with pytest.raises(DataContractError):
        BarWindow("A", SESSIONS[1], after(SESSIONS[1]), tuple(bars), PriceBasis.RAW, (), "sip")


@pytest.mark.parametrize(
    ("skip_index", "issue"),
    [(5, WindowIssue.MISSING_SESSIONS), (29, WindowIssue.DECISION_SESSION_MISSING)],
)
def test_missing_sessions_are_never_filled(skip_index: int, issue: WindowIssue) -> None:
    bars = _series(30)
    del bars[skip_index]
    prep = prepare_series("ABC", bars, calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert prep.series is not None
    res = prep.series.window(CALENDAR, SESSIONS[SESSIONS.index(date(2026, 3, 2)) + 29], 30)
    assert res.status is WindowStatus.INSUFFICIENT and res.issue is issue
    assert res.window is None


def test_recent_listing_and_short_calendar() -> None:
    prep = prepare_series("ABC", _series(10), calendar=CALENDAR, corporate_actions=attested("ABC"))
    assert prep.series is not None
    end = SESSIONS[SESSIONS.index(date(2026, 3, 2)) + 9]
    assert prep.series.window(CALENDAR, end, 20).issue is WindowIssue.HISTORY_STARTS_IN_WINDOW
    assert prep.series.window(CALENDAR, SESSIONS[2], 20).issue is WindowIssue.CALENDAR_TOO_SHORT
    assert prep.series.window(CALENDAR, date(2026, 3, 7), 5).issue is WindowIssue.NOT_A_SESSION


# --------------------------------------------------------------------- splits
def _pit_split_series() -> tuple[list[DailyBar], date]:
    """Raw prices: 100 before a 2-for-1 split, 50 from the split session on."""
    i = SESSIONS.index(date(2026, 3, 2))
    days = SESSIONS[i : i + 20]
    split_day = days[10]
    out = []
    for d in days:
        px = D(100) if d < split_day else D(50)
        vol = 1_000 if d < split_day else 2_000
        out.append(bar("ABC", d, candle(px, px, D(1) if d < split_day else D("0.5")), volume=vol))
    return out, split_day


def test_point_in_time_split_adjustment_has_no_look_ahead() -> None:
    bars, split_day = _pit_split_series()
    split = SplitEvent("ABC", split_day, 2, 1)
    prep = prepare_series("ABC", bars, calendar=CALENDAR,
                          corporate_actions=attested("ABC", [split]))  # fmt: skip
    assert prep.series is not None
    # decision BEFORE the split: raw prices; the later split is not visible
    before = prep.series.window(CALENDAR, CALENDAR.dates[CALENDAR.index(split_day) - 1], 8).window
    assert before is not None and before.basis is PriceBasis.RAW
    assert {b.close for b in before.bars} == {D(100)}
    assert before.splits_applied == ()
    # decision AFTER the split: pre-split bars halved, volume doubled; latest bar raw
    after_w = prep.series.window(CALENDAR, bars[-1].session_date, 20).window
    assert after_w is not None and after_w.basis is PriceBasis.POINT_IN_TIME_SPLIT_ADJUSTED
    assert after_w.splits_applied == (split,)
    assert {b.close for b in after_w.bars} == {D(50)}
    assert {b.volume for b in after_w.bars} == {D(2000)}
    assert after_w.bars[-1] == StrategyBar("ABC", bars[-1].session_date, bars[-1].open,
                                           bars[-1].high, bars[-1].low, bars[-1].close,
                                           D(bars[-1].volume))  # fmt: skip
    # a window that starts on the split session needs no adjustment
    on = prep.series.window(CALENDAR, bars[-1].session_date, 10).window
    assert on is not None and on.basis is PriceBasis.RAW


def test_three_for_two_split_adjusts_exactly() -> None:
    bars, split_day = _pit_split_series()
    actions = attested("ABC", [SplitEvent("ABC", split_day, 3, 2)])
    prep = prepare_series("ABC", bars, calendar=CALENDAR, corporate_actions=actions)
    assert prep.series is not None
    w = prep.series.window(CALENDAR, bars[-1].session_date, 20).window
    assert w is not None
    first = w.bars[0]
    assert first.close * Fraction(3, 2).numerator == D(100) * 2  # 100 x 2/3 exactly
    assert first.volume == D(1500)


def _fixture_rows(adjustment: str, rows: list[dict[str, object]]) -> list[DailyBar]:
    out = []
    for row in rows:
        b = daily_bar_from_raw(row, "SPL", feed="sip", adjustment=adjustment,  # type: ignore[arg-type]
                               received_at=received(date.fromisoformat("2026-12-01")))  # fmt: skip
        out.append(b)
    return out


def test_adjusted_versus_raw_bars_derive_the_split_and_match_point_in_time() -> None:
    days = bars_fixture.sessions(date(2026, 10, 1), date(2026, 11, 30))
    raw_rows, adj_rows = bars_fixture.split_pair(days)
    raw = _fixture_rows("raw", raw_rows)
    adjusted = _fixture_rows("split", adj_rows)
    derivation = derive_split_events("SPL", raw, adjusted)
    assert derivation.resolved
    assert [(e.effective_session, e.new_shares, e.old_shares) for e in derivation.events] == [
        (bars_fixture.SPLIT_DAY, 2, 1)
    ]
    prep = prepare_series("SPL", raw, calendar=CALENDAR,
                          corporate_actions=attested("SPL", derivation.events))  # fmt: skip
    assert prep.series is not None
    w = prep.series.window(CALENDAR, days[-1], len(days)).window
    assert w is not None
    # point-in-time adjustment of raw bars reproduces the provider's adjusted series
    for got, want in zip(w.bars, adjusted, strict=True):
        assert (got.open, got.high, got.low, got.close) == (want.open, want.high, want.low,
                                                            want.close)  # fmt: skip
    # ... while the provider's adjusted bars are NOT what was tradable before the split
    pre = next(b for b in raw if b.session_date < bars_fixture.SPLIT_DAY)
    pre_adj = next(b for b in adjusted if b.session_date == pre.session_date)
    assert pre.close == pre_adj.close * 2


def test_split_derivation_fails_closed() -> None:
    days = bars_fixture.sessions(date(2026, 10, 1), date(2026, 10, 30))
    raw_rows = bars_fixture.history(days)
    raw = _fixture_rows("raw", raw_rows)
    adjusted = _fixture_rows("split", raw_rows)
    assert derive_split_events("SPL", raw, adjusted).events == ()  # no split, no event
    # a 3% factor change (dividend-like) is not a plausible split ratio
    odd = [replace(b, open=b.open * D("0.97"), high=b.high * D("0.97"), low=b.low * D("0.97"),
                   close=b.close * D("0.97")) if b.session_date < days[10] else b
           for b in adjusted]  # fmt: skip
    res = derive_split_events("SPL", raw, odd)
    assert not res.resolved
    assert {f.issue for f in res.findings} == {DataIssue.SPLIT_RATIO_UNRESOLVED}
    # coverage and adjustment mismatches
    assert not derive_split_events("SPL", raw, adjusted[:-1]).resolved
    assert not derive_split_events("SPL", raw, raw).resolved
    # reverse split 1-for-10 is plausible
    rev = [replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10)
           if b.session_date < days[10] else b for b in adjusted]  # fmt: skip
    rev_res = derive_split_events("SPL", raw, rev)
    assert [(e.new_shares, e.old_shares) for e in rev_res.events] == [(1, 10)]


def test_canonical_scenario_is_a_valid_series() -> None:
    sc = setup_scenario()
    prep = prepared(sc)
    assert prep.accepted and prep.findings == ()
    assert sc.decision_session == sc.days[-1]
    assert sc.decision_session - sc.days[0] > timedelta(days=100)


# --------------------------------------------------------------------- corporate-action contract
def _issues(prep: SeriesPreparation) -> set[DataIssue]:
    return {f.issue for f in prep.findings}


def _prep(
    actions: CorporateActions | None,
    bars: list[DailyBar] | None = None,
    policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE,
) -> SeriesPreparation:
    return prepare_series("ABC", bars or _series(), calendar=CALENDAR, corporate_actions=actions,
                          dividend_policy=policy)  # fmt: skip


def test_no_record_is_not_read_as_no_corporate_actions() -> None:
    prep = _prep(None)
    assert not prep.accepted and _issues(prep) == {DataIssue.CORPORATE_ACTIONS_MISSING}


def test_empty_event_lists_need_attested_coverage() -> None:
    unattested = CorporateActions("ABC")  # no events, but nothing attests "none occurred"
    prep = _prep(unattested)
    assert not prep.accepted
    assert {DataIssue.SPLIT_COVERAGE_MISSING, DataIssue.DIVIDEND_COVERAGE_MISSING} <= _issues(prep)
    assert _prep(attested("ABC")).accepted


def test_coverage_must_span_every_bar() -> None:
    bars = _series()
    short = attested("ABC", end=bars[-2].session_date)
    prep = _prep(short, bars)
    assert not prep.accepted
    assert {DataIssue.SPLIT_COVERAGE_GAP, DataIssue.DIVIDEND_COVERAGE_GAP} <= _issues(prep)
    late = attested("ABC", start=bars[1].session_date)
    assert DataIssue.SPLIT_COVERAGE_GAP in _issues(_prep(late, bars))
    window = CoverageWindow(bars[0].session_date, bars[-1].session_date, "x")
    only_splits = CorporateActions("ABC", split_coverage=window)
    assert _issues(_prep(only_splits, bars)) == {DataIssue.DIVIDEND_COVERAGE_MISSING}


def test_record_for_another_symbol_is_rejected() -> None:
    assert DataIssue.CORPORATE_ACTIONS_SYMBOL_MISMATCH in _issues(_prep(attested("XYZ")))


def test_events_outside_coverage_and_invalid_events_fail_closed() -> None:
    bars = _series()
    window = CoverageWindow(bars[0].session_date, bars[-1].session_date, "x")
    outside = CashDividend("ABC", date(2026, 6, 1), date(2026, 6, 5), D("0.10"))
    prep = _prep(CorporateActions("ABC", (), (outside,), window, window), bars)
    assert DataIssue.CORPORATE_ACTION_OUTSIDE_COVERAGE in _issues(prep)
    saturday = CashDividend("ABC", date(2026, 3, 7), date(2026, 3, 10), D("0.10"))
    assert DataIssue.DIVIDEND_EVENT_INVALID in _issues(_prep(attested("ABC", (), [saturday])))
    d = bars[5].session_date
    twice = [CashDividend("ABC", d, d, D("0.1")), CashDividend("ABC", d, d, D("0.2"))]
    assert DataIssue.DIVIDEND_EVENT_INVALID in _issues(_prep(attested("ABC", (), twice)))
    with pytest.raises(DataContractError):
        CashDividend("ABC", d, d - timedelta(days=1), D("0.1"))  # paid before the ex-date
    with pytest.raises(DataContractError):
        CashDividend("ABC", d, d, 0.1)  # type: ignore[arg-type]
    with pytest.raises(DataContractError):
        CoverageWindow(d, d - timedelta(days=1), "x")


def test_split_and_dividend_on_the_same_session_conflict() -> None:
    bars, split_day = _pit_split_series()
    actions = attested("ABC", [SplitEvent("ABC", split_day, 2, 1)],
                       [CashDividend("ABC", split_day, split_day, D("0.5"))])  # fmt: skip
    assert DataIssue.CORPORATE_ACTION_CONFLICT in _issues(_prep(actions, bars))


def test_recorded_split_must_show_in_raw_prices() -> None:
    bars = _series()  # continuous prices: no split happened
    actions = attested("ABC", [SplitEvent("ABC", bars[10].session_date, 2, 1)])
    prep = _prep(actions, bars)
    assert not prep.accepted
    assert DataIssue.SPLIT_NOT_REFLECTED_IN_PRICES in _issues(prep)


def test_unrecorded_reverse_split_like_rise_rejects_the_series() -> None:
    bars = _series()
    jumped = [*bars[:10], *[replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10,
                                    close=b.close * 10) for b in bars[10:]]]  # fmt: skip
    prep = _prep(attested("ABC"), jumped)
    assert not prep.accepted and DataIssue.SPLIT_SUSPECTED_UNRECORDED in _issues(prep)


def test_unrecorded_split_like_drop_is_kept_but_never_spanned_by_a_window() -> None:
    bars, split_day = _pit_split_series()  # 2:1 drop, deliberately NOT recorded
    prep = _prep(attested("ABC"), bars)
    assert prep.accepted and prep.series is not None
    assert _issues(prep) == {DataIssue.PRICE_DISCONTINUITY_UNEXPLAINED}
    assert prep.series.suspect_sessions == frozenset({split_day})
    spans = prep.series.window(CALENDAR, bars[-1].session_date, 15)
    assert spans.issue is WindowIssue.CORPORATE_ACTION_SUSPECT and spans.window is None
    after_only = prep.series.window(CALENDAR, bars[-1].session_date, 10)  # starts at split_day
    assert after_only.status is WindowStatus.COMPLETE
    before = prep.series.window(CALENDAR, CALENDAR.dates[CALENDAR.index(split_day) - 1], 10)
    assert before.status is WindowStatus.COMPLETE


def test_price_return_only_must_be_chosen_explicitly() -> None:
    no_div_coverage = CorporateActions(
        "ABC", split_coverage=CoverageWindow(date(2026, 1, 2), date(2026, 12, 31), "x")
    )
    assert not _prep(no_div_coverage).accepted
    div = CashDividend("ABC", date(2026, 3, 10), date(2026, 3, 12), D("0.1"))
    prep = _prep(replace(no_div_coverage, dividends=(div,)),
                 policy=DividendPolicy.PRICE_RETURN_ONLY)  # fmt: skip
    assert prep.accepted and prep.series is not None
    assert prep.series.dividends == () and not prep.series.dividends_included
    assert _issues(prep) == {DataIssue.DIVIDENDS_EXCLUDED}


def test_digest_covers_corporate_actions_and_coverage() -> None:
    base = _prep(attested("ABC"))
    div = CashDividend("ABC", date(2026, 3, 10), date(2026, 3, 12), D("0.1"))
    with_div = _prep(attested("ABC", (), [div]))
    wider = _prep(attested("ABC", end=date(2026, 12, 30)))
    digests = {p.series.provenance.digest for p in (base, with_div, wider) if p.series}
    assert len(digests) == 3


def test_derived_split_coverage_attests_the_compared_sessions() -> None:
    days = bars_fixture.sessions(date(2026, 10, 1), date(2026, 11, 30))
    raw_rows, adj_rows = bars_fixture.split_pair(days)
    raw, adjusted = _fixture_rows("raw", raw_rows), _fixture_rows("split", adj_rows)
    derivation = derive_split_events("SPL", raw, adjusted)
    assert derivation.coverage == CoverageWindow(days[0], days[-1], "derived:raw-vs-split")
    unresolved = derive_split_events("SPL", raw, adjusted[:-1])
    assert unresolved.coverage is None
    actions = CorporateActions("SPL", derivation.events, (), derivation.coverage,
                               attested("SPL").dividend_coverage)  # fmt: skip
    assert prepare_series("SPL", raw, calendar=CALENDAR, corporate_actions=actions).accepted
