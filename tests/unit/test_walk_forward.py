"""Walk-forward folds, point-in-time views and holdout isolation (Step 16.15, Phase 4)."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.research.dataset import load_dataset
from swing_trading_bot.strategy.backtest import (
    BacktestConfig,
    EntryTiming,
    ExecutionAssumptions,
    ExitPolicyMode,
)
from swing_trading_bot.strategy.market_data import (
    CashDividend,
    CorporateActions,
    CoverageWindow,
    SplitEvent,
)
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from swing_trading_bot.strategy.walk_forward import (
    DEFAULT_PERIODS,
    RESEARCH_READY,
    DataView,
    DateRange,
    FixedDefaultsSelector,
    FoldDesign,
    FoldKind,
    FoldStatus,
    HoldoutAccessError,
    HoldoutRelease,
    PortfolioMode,
    ResearchAdmission,
    ResearchInputs,
    ResearchPeriods,
    SealedResearchData,
    WalkForwardError,
    WalkForwardProtocol,
    WindowScheme,
    add_months,
    build_folds,
    clip_corporate_actions,
    fold_table,
    freeze_protocol,
    holdout_fold,
    run_holdout,
    run_walk_forward,
)
from tests.support.research_fixture import (
    DATES,
    SESSIONS,
    FixtureSpec,
    daily_bar,
    default_spec,
    synthetic_bars,
    write_fixture,
)
from tests.support.strategy_fixture import LIMITS, ZERO_FEES

HOLDOUT = DEFAULT_PERIODS.holdout.start
CRAZY = (Decimal(200), Decimal(260), Decimal(150), Decimal(255))
SECTORS = {"AAA": "Tech", "BBB": "Energy", "CCC": "Tech"}


def _inputs(tmp_path: Path, spec: FixtureSpec | None = None, name: str = "ds") -> ResearchInputs:
    write_fixture(tmp_path / name, spec)
    return load_dataset(tmp_path / name).research_inputs(SECTORS)


def _sealed(
    tmp_path: Path, spec: FixtureSpec | None = None, name: str = "ds"
) -> SealedResearchData:
    return SealedResearchData(_inputs(tmp_path, spec, name), holdout_start=HOLDOUT)


def _protocol(design: FoldDesign | None = None,
              periods: ResearchPeriods = DEFAULT_PERIODS) -> WalkForwardProtocol:  # fmt: skip
    template = BacktestConfig(SupportResistanceParams(), ExitTemplate(),
                              ExitPolicyMode.PROTECTION_FIRST,
                              ExecutionAssumptions(fees=ZERO_FEES), LIMITS,
                              periods.development.start, periods.development.start)  # fmt: skip
    return WalkForwardProtocol(periods, design or FoldDesign(), template, "fixed-defaults-v1")


# ===================================================================== dates and folds


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2021, 1, 31), 1, date(2021, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2021, 3, 15), -3, date(2020, 12, 15)),
        (date(2021, 12, 1), 1, date(2022, 1, 1)),
        (date(2021, 1, 1), 12, date(2022, 1, 1)),
    ],
)
def test_add_months(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected


def test_periods_must_be_ordered_and_disjoint() -> None:
    dev = DateRange(date(2021, 1, 1), date(2023, 12, 31))
    with pytest.raises(WalkForwardError):
        ResearchPeriods(dev, DateRange(date(2023, 6, 1), date(2024, 12, 31)),
                        DateRange(date(2025, 1, 1), date(2026, 10, 31)))  # fmt: skip
    with pytest.raises(WalkForwardError):
        ResearchPeriods(dev, DateRange(date(2024, 1, 1), date(2025, 1, 1)),
                        DateRange(date(2025, 1, 1), date(2026, 10, 31)))  # fmt: skip
    with pytest.raises(WalkForwardError):
        DateRange(date(2022, 1, 2), date(2022, 1, 1))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"step_months": 3, "test_months": 6},
        {"scheme": WindowScheme.ROLLING},
        {"rolling_train_months": 12},
        {"embargo_sessions": -1},
        {"initial_train_months": 0},
    ],
)
def test_fold_design_rejects_invalid_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(WalkForwardError):
        FoldDesign(**kwargs)  # type: ignore[arg-type]


def test_default_folds_are_chronological_embargoed_and_never_reach_the_holdout() -> None:
    folds = build_folds(DEFAULT_PERIODS, FoldDesign(), DATES)
    assert [f.kind for f in folds] == [FoldKind.WALK_FORWARD] * 4 + [FoldKind.VALIDATION]
    assert [(f.test_start, f.test_end) for f in folds] == [
        (date(2022, 1, 3), date(2022, 6, 30)),
        (date(2022, 7, 1), date(2022, 12, 30)),
        (date(2023, 1, 2), date(2023, 6, 30)),
        (date(2023, 7, 3), date(2023, 12, 29)),
        (date(2024, 1, 2), date(2024, 12, 31)),
    ]
    for f in folds:
        assert f.train_start == date(2021, 1, 4)  # expanding from the development start
        assert len(f.embargo) == 5
        assert f.train_end < f.embargo[0] <= f.embargo[-1] < f.test_start
        assert f.test_end < HOLDOUT
    for a, b in itertools.pairwise(folds):
        assert a.test_end < b.test_start  # test windows never overlap
    assert fold_table(folds) == fold_table(build_folds(DEFAULT_PERIODS, FoldDesign(), DATES))


def test_rolling_folds_bound_the_training_window() -> None:
    design = FoldDesign(scheme=WindowScheme.ROLLING, rolling_train_months=12)
    folds = build_folds(DEFAULT_PERIODS, design, DATES)
    assert folds[0].train_start == date(2021, 1, 4)
    assert folds[2].train_start == date(2022, 1, 3)  # 12 months before 2023-01-02
    assert folds[-1].train_start == date(2023, 1, 2)


def test_holdout_fold_trains_on_everything_before_it() -> None:
    fold = holdout_fold(DEFAULT_PERIODS, FoldDesign(), DATES)
    assert fold.kind is FoldKind.HOLDOUT
    assert fold.test_start == date(2025, 1, 2)
    assert fold.test_end == date(2026, 10, 30)
    assert fold.train_end < fold.test_start
    assert fold.train_start == date(2021, 1, 4)


# ===================================================================== views


def test_holdout_is_sealed_without_a_release(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    assert sealed.view(date(2024, 12, 31)).includes_holdout is False
    for until in (HOLDOUT, date(2025, 6, 2), date(2026, 10, 30)):
        with pytest.raises(HoldoutAccessError):
            sealed.view(until)
    wrong = HoldoutRelease("other-dataset", "p", "x", "reviewer", "final", date(2026, 11, 16))
    with pytest.raises(HoldoutAccessError):
        sealed.view(HOLDOUT, release=wrong)
    right = replace(wrong, dataset_digest=sealed.dataset_digest)
    assert sealed.view(date(2026, 10, 30), release=right).includes_holdout is True


def test_release_needs_an_approver_and_a_reason() -> None:
    with pytest.raises(WalkForwardError):
        HoldoutRelease("d", "p", "x", " ", "reason", date(2026, 11, 16))


def test_view_physically_removes_the_future(tmp_path: Path) -> None:
    spec = default_spec()
    rec = spec.records["AAA"]
    assert rec is not None
    spec.records["AAA"] = replace(rec, dividends=(
        CashDividend("AAA", date(2022, 3, 1), date(2022, 3, 15), Decimal("0.25")),
        CashDividend("AAA", date(2024, 3, 1), date(2024, 3, 15), Decimal("0.25")),
    ))  # fmt: skip
    sealed = _sealed(tmp_path, spec)
    view = sealed.view(date(2023, 6, 30))
    assert view.calendar.last == date(2023, 6, 30)
    for prep in view.preparations:
        assert prep.series is not None
        assert prep.series.bars[-1].session_date == date(2023, 6, 30)
    aaa = view.preparations[0].series
    assert aaa is not None
    assert [d.ex_session for d in aaa.dividends] == [date(2022, 3, 1)]


def test_clip_corporate_actions_drops_later_events_and_clips_coverage(tmp_path: Path) -> None:
    ca = _inputs(tmp_path).corporate_actions["AAA"]
    assert ca is not None
    ca = replace(ca, splits=(SplitEvent("AAA", date(2024, 6, 3), 2, 1),))
    clipped = clip_corporate_actions(ca, date(2023, 12, 29))
    assert clipped is not None
    assert clipped.splits == ()
    assert clipped.split_coverage is not None
    assert clipped.split_coverage.end == date(2023, 12, 29)
    assert clip_corporate_actions(ca, date(2019, 1, 1)) == replace(
        ca, splits=(), dividends=(), split_coverage=None, dividend_coverage=None)  # fmt: skip
    assert clip_corporate_actions(None, date(2023, 1, 3)) is None


# ===================================================================== runs


class SpySelector:
    """Records what parameter selection was shown and tries to read past it."""

    def __init__(self) -> None:
        self.seen: list[tuple[date, date, bool]] = []

    @property
    def selector_id(self) -> str:
        return "fixed-defaults-v1"

    def select(self, training: DataView,
               defaults: SupportResistanceParams) -> SupportResistanceParams:  # fmt: skip
        last_bar = max(p.series.bars[-1].session_date for p in training.preparations
                       if p.series is not None)  # fmt: skip
        self.seen.append((training.until, last_bar, training.includes_holdout))
        return defaults


def test_selection_sees_only_training_data_before_the_embargo(tmp_path: Path) -> None:
    spy = SpySelector()
    report = run_walk_forward(_sealed(tmp_path), _protocol(), spy)
    assert len(spy.seen) == len(report.folds) == 5
    for (until, last_bar, holdout), result in zip(spy.seen, report.folds, strict=True):
        fold = result.fold
        assert until == last_bar == fold.train_end == result.selection_data_until
        assert last_bar < fold.embargo[0]
        assert holdout is False
        assert result.test_data_until == fold.test_end < HOLDOUT
    assert report.holdout_evaluated is False


def test_walk_forward_never_requests_a_holdout_view(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    requested: list[date] = []
    original = sealed.view

    class Recording(SealedResearchData):
        __slots__ = ()

        def view(self, until: date, *, release: HoldoutRelease | None = None) -> DataView:
            requested.append(until)
            return original(until, release=release)

    rec = Recording(_inputs(tmp_path, name="ds2"), holdout_start=HOLDOUT)
    run_walk_forward(rec, _protocol(), FixedDefaultsSelector())
    assert requested
    assert max(requested) < HOLDOUT


def test_future_and_holdout_bars_cannot_change_walk_forward_results(tmp_path: Path) -> None:
    base = default_spec()
    altered = default_spec()
    for sym, bars in altered.bars.items():  # wildly different prices from 2024-07 on
        altered.bars[sym] = [
            b if b.session_date < date(2024, 7, 1)
            else daily_bar(sym, b.session_date, CRAZY)
            for b in bars
        ]  # fmt: skip
    a = run_walk_forward(_sealed(tmp_path, base, "a"), _protocol(), FixedDefaultsSelector())
    b = run_walk_forward(_sealed(tmp_path, altered, "b"), _protocol(), FixedDefaultsSelector())
    assert a.dataset_digest != b.dataset_digest
    for fa, fb in zip(a.folds[:4], b.folds[:4], strict=True):  # folds ending before 2024-07
        assert fa.executable is not None and fb.executable is not None
        assert fa.hypothetical is not None and fb.hypothetical is not None
        assert fa.executable.backtest_fingerprint == fb.executable.backtest_fingerprint
        assert fa.hypothetical.backtest_fingerprint == fb.hypothetical.backtest_fingerprint
    va, vb = a.folds[4].hypothetical, b.folds[4].hypothetical
    assert va is not None and vb is not None
    assert va.backtest_fingerprint != vb.backtest_fingerprint  # the 2024 test window differs


def test_walk_forward_is_deterministic_and_reports_both_policies(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    one = run_walk_forward(sealed, _protocol(), FixedDefaultsSelector())
    two = run_walk_forward(sealed, _protocol(), FixedDefaultsSelector())
    assert one.report_fingerprint == two.report_fingerprint
    assert one.protocol_fingerprint == freeze_protocol(sealed, _protocol()).fingerprint
    assert one.portfolio_mode is PortfolioMode.INDEPENDENT_FOLDS
    for r in one.folds:
        assert r.status is FoldStatus.EVALUATED
        assert r.executable is not None and r.hypothetical is not None
        assert r.executable.policy == "PROTECTION_FIRST" and r.executable.executable_today
        assert r.hypothetical.policy == "HYPOTHETICAL_LINKED_EXITS"
        assert not r.hypothetical.executable_today
    assert [a.policy for a in one.aggregates] == ["PROTECTION_FIRST", "HYPOTHETICAL_LINKED_EXITS"]


def test_frozen_protocol_covers_every_research_choice(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    base_protocol = _protocol()
    frozen = freeze_protocol(sealed, base_protocol)
    base = frozen.fingerprint
    assert frozen.identity["dataset_digest"] == sealed.dataset_digest
    assert frozen.identity["file_checksums"]  # every dataset file, by SHA-256
    assert frozen.identity["universe_id"] == "fixture"
    assert frozen.strategy["strategy_version"] and frozen.strategy["engine_version"]
    assert frozen.parameters_fingerprint == base_protocol.template.params.fingerprint
    assert [f["kind"] for f in frozen.folds] == ["WALK_FORWARD"] * 4 + ["VALIDATION", "HOLDOUT"]
    assert frozen.metrics["metrics_version"] and frozen.release_conditions
    assert frozen.execution["assumptions"]["entry_slippage_bps"] == "5"  # type: ignore[index]
    template = base_protocol.template
    ax = template.assumptions
    variants = [
        replace(base_protocol, design=FoldDesign(embargo_sessions=6)),
        replace(base_protocol, template=replace(
            template, params=SupportResistanceParams(min_reward_risk=Decimal("2.5")))),
        replace(base_protocol, template=replace(
            template, assumptions=replace(ax, stop_slippage_bps=Decimal(20)))),
        replace(base_protocol, template=replace(
            template, assumptions=replace(ax, entry_timing=EntryTiming.RESTING_BEFORE_OPEN))),
        replace(base_protocol, template=replace(template, initial_equity=Decimal(6000))),
        replace(base_protocol, min_completed_trades=50),
        replace(base_protocol, selector_id="tuned-v1"),
        replace(base_protocol, portfolio_mode=PortfolioMode.CONTINUOUS),
    ]  # fmt: skip
    prints = {freeze_protocol(sealed, v).fingerprint for v in variants}
    assert base not in prints and len(prints) == len(variants)
    moved = replace(template, start=date(2022, 1, 3), end=date(2022, 2, 1))
    assert freeze_protocol(sealed, replace(base_protocol, template=moved)).fingerprint == base
    inputs = _inputs(tmp_path, name="other")
    for changed in (replace(inputs, file_checksums={"bars/AAA.raw.jsonl": "0" * 64}),
                    replace(inputs, universe_fingerprint="f" * 64)):  # fmt: skip
        other = SealedResearchData(changed, holdout_start=HOLDOUT)
        assert freeze_protocol(other, base_protocol).fingerprint != base


def test_a_different_selector_is_refused(tmp_path: Path) -> None:
    class Other(FixedDefaultsSelector):
        @property
        def selector_id(self) -> str:
            return "tuned-v1"

    with pytest.raises(WalkForwardError):
        run_walk_forward(_sealed(tmp_path), _protocol(), Other())


def test_insufficient_warmup_and_short_test_windows_are_reported_not_weakened(
    tmp_path: Path,
) -> None:
    sealed = _sealed(tmp_path)
    early = ResearchPeriods(DateRange(date(2020, 7, 1), date(2023, 12, 31)),
                            DEFAULT_PERIODS.validation, DEFAULT_PERIODS.holdout)  # fmt: skip
    design = FoldDesign(initial_train_months=1, test_months=6, step_months=6,
                        embargo_sessions=0)  # fmt: skip
    report = run_walk_forward(sealed, _protocol(design, early), FixedDefaultsSelector())
    first = report.folds[0]
    assert first.status is FoldStatus.INSUFFICIENT_WARMUP
    assert first.executable is None and first.hypothetical is None
    assert "< 60 sessions" in first.detail
    short = run_walk_forward(sealed, _protocol(FoldDesign(min_test_sessions=200)),
                             FixedDefaultsSelector())  # fmt: skip
    assert [f.status for f in short.folds][:4] == [FoldStatus.INSUFFICIENT_TEST_SESSIONS] * 4
    assert short.folds[4].status is FoldStatus.EVALUATED  # the 12-month validation window


def _release(sealed: SealedResearchData, protocol: WalkForwardProtocol) -> HoldoutRelease:
    report = run_walk_forward(sealed, protocol, FixedDefaultsSelector())
    return HoldoutRelease(sealed.dataset_digest, freeze_protocol(sealed, protocol).fingerprint,
                          report.report_fingerprint, "reviewer", "final evaluation",
                          date(2026, 11, 16))  # fmt: skip


def test_holdout_runs_only_with_a_matching_release(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    protocol = _protocol()
    good = _release(sealed, protocol)
    for bad in (replace(good, frozen_protocol_fingerprint="x"),
                replace(good, walk_forward_report_fingerprint="x"),
                replace(good, dataset_digest="x")):  # fmt: skip
        with pytest.raises(HoldoutAccessError):
            run_holdout(sealed, protocol, FixedDefaultsSelector(), bad)
    changed = replace(protocol, design=FoldDesign(embargo_sessions=10))
    with pytest.raises(HoldoutAccessError):  # any protocol change after the release voids it
        run_holdout(sealed, changed, FixedDefaultsSelector(), good)
    costlier = replace(protocol, template=replace(protocol.template, assumptions=replace(
        protocol.template.assumptions, entry_slippage_bps=Decimal(6))))  # fmt: skip
    with pytest.raises(HoldoutAccessError):
        run_holdout(sealed, costlier, FixedDefaultsSelector(), good)
    report = run_holdout(sealed, protocol, FixedDefaultsSelector(), good)
    assert report.holdout_evaluated is True
    assert report.holdout_release == good.fingerprint
    assert report.protocol_fingerprint == good.frozen_protocol_fingerprint
    (fold,) = report.folds
    assert fold.fold.kind is FoldKind.HOLDOUT
    assert fold.selection_data_until < HOLDOUT  # selection still never sees the holdout
    assert fold.test_data_until == date(2026, 10, 30)


def test_holdout_selection_never_sees_the_holdout(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    good = _release(sealed, _protocol())
    spy = SpySelector()
    run_holdout(sealed, _protocol(), spy, good)
    assert spy.seen  # walk-forward reproduction + the holdout fold's selection
    assert all(until < HOLDOUT and not holdout for until, _, holdout in spy.seen)


def test_fixed_selector_refuses_holdout_data(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    release = HoldoutRelease(sealed.dataset_digest, "p", "x", "r", "why", date(2026, 11, 16))
    with pytest.raises(HoldoutAccessError):
        FixedDefaultsSelector().select(sealed.view(HOLDOUT, release=release),
                                       SupportResistanceParams())  # fmt: skip


def test_research_inputs_require_matching_symbols(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    with pytest.raises(WalkForwardError):
        replace(inputs, corporate_actions={"AAA": None})


# ===================================================================== property

_WINDOW = CoverageWindow(DATES[0], DATES[-1], "fixture:attested")
_SPLIT_DAY = date(2024, 6, 3)


def _split_bars(symbol: str, phase: int) -> tuple[DailyBar, ...]:
    """Prices halve at the 2:1 split session (the split shows in raw prices)."""
    before = synthetic_bars(symbol, scale=Decimal(2), phase=phase)
    after = synthetic_bars(symbol, phase=phase)
    return tuple(
        b if b.session_date < _SPLIT_DAY else a for b, a in zip(before, after, strict=True)
    )


_PROP_INPUTS = ResearchInputs(
    dataset_digest="p" * 64,
    admission=ResearchAdmission("p" * 64, RESEARCH_READY, "r" * 64),
    sessions=SESSIONS,
    bars={s: _split_bars(s, i * 5) for i, s in enumerate(("AAA", "BBB"))},
    corporate_actions={
        s: CorporateActions(
            s, (SplitEvent(s, _SPLIT_DAY, 2, 1),),
            (CashDividend(s, date(2022, 3, 1), date(2022, 3, 15), Decimal("0.1")),),
            _WINDOW, _WINDOW)
        for s in ("AAA", "BBB")
    },
)  # fmt: skip
_PROP_SEALED = SealedResearchData(_PROP_INPUTS, holdout_start=HOLDOUT)
_PRE_HOLDOUT = tuple(d for d in DATES if d < HOLDOUT)


@given(st.sampled_from(_PRE_HOLDOUT))
@example(_SPLIT_DAY)
@example(date(2024, 5, 31))
@settings(max_examples=25, deadline=None)
def test_views_never_contain_anything_after_until(until: date) -> None:
    view = _PROP_SEALED.view(until)
    assert view.calendar.last == until
    for prep in view.preparations:
        series = prep.series
        assert series is not None
        assert all(b.session_date <= until for b in series.bars)
        assert all(s.effective_session <= until for s in series.splits)
        assert all(d.ex_session <= until for d in series.dividends)
