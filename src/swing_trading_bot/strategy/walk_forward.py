"""Chronological walk-forward evaluation with an isolated final holdout (Step 16.15, M2-4).

Pure and deterministic; no I/O and no clock. The inputs are an in-memory
:class:`ResearchInputs` (raw daily bars, corporate actions and the session calendar —
loaded and integrity-checked elsewhere) and a :class:`WalkForwardProtocol`.

**Periods.** :class:`ResearchPeriods` splits the research window into *development*,
*validation* and a final *holdout*, strictly in that order and non-overlapping.

**Folds** (:func:`build_folds`). Walk-forward folds lie inside the development period:
the first test window starts ``initial_train_months`` after the development start, each
test window is ``test_months`` long and windows advance by ``step_months``
(``>= test_months``: test windows never overlap). Training is EXPANDING (from the
development start) or ROLLING (the last ``rolling_train_months``). A final VALIDATION fold
tests the validation period with training from development data. The HOLDOUT fold
(:func:`holdout_fold`) is built separately and evaluated only by :func:`run_holdout`.

* *Embargo / purge.* ``embargo_sessions`` sessions separate the last training session
  from the first test session. Training data is truncated at the training end, so no
  training-time computation (parameter selection, in-sample marks of positions still
  open) can use a price from the embargo or the test window. Every test window starts
  flat with fresh capital: no position or label crosses a fold boundary.
* *Warm-up.* A test window may use earlier bars as decision history (past data), but
  nothing is traded before the test start, and a fold whose calendar holds fewer than
  ``params.required_sessions`` sessions before the test start is
  ``INSUFFICIENT_WARMUP`` and is not evaluated (the methodology is never weakened to
  produce a number).
* *Test windows.* A window with fewer than ``min_test_sessions`` sessions is
  ``INSUFFICIENT_TEST_SESSIONS``.

**No future data.** :meth:`SealedResearchData.view` returns physically truncated data:
bars, calendar sessions and corporate-action events after ``until`` are removed and
coverage windows are clipped at ``until``. A decision can therefore not see a later bar,
a later split or a later dividend, whatever the strategy code does.

**Holdout isolation.** A view reaching the holdout start or later raises
:class:`HoldoutAccessError` unless a :class:`HoldoutRelease` for this dataset is passed.
Parameter selection receives only a training :class:`DataView` (no reference back to the
sealed data), always ending before the fold's embargo. :func:`run_walk_forward` never
requests a holdout view; :func:`run_holdout` requires a release naming the current
:class:`FrozenProtocol` fingerprint (dataset and file checksums, universe, strategy and
engine versions, parameters, execution policy and costs, folds, metrics, selection
procedure, release conditions) and the walk-forward report that protocol reproduces, so
changing anything after the release invalidates it.

**Research admission** (Step 16.16E.1). :class:`SealedResearchData` — and therefore
every evaluation entry point (:func:`freeze_protocol`, :func:`run_walk_forward`,
:func:`run_continuous`, :func:`run_holdout`) — refuses inputs without a
:class:`ResearchAdmission` for the same dataset in state ``RESEARCH_READY``
(:class:`ResearchAdmissionError`). The research layer issues one only from a dataset
that passed both readiness gates (``research.dataset.assess_readiness``); its readiness
fingerprint is part of the frozen protocol, so a holdout release is bound to it.

**Portfolio state at fold boundaries** (:class:`PortfolioMode`). ``INDEPENDENT_FOLDS``
(default): every test window starts flat with the template's initial equity and no
pending orders, unsettled proceeds or receivables; its end state is reported
(``PerformanceReport.end_state``) and discarded. Fold returns are never chained.
``CONTINUOUS``: :func:`run_continuous` replays all walk-forward and validation test
windows as one portfolio, accepted only when one replay represents it exactly (identical
parameters in every fold, contiguous windows); anything else is refused.

**Parameter selection.** Only :class:`FixedDefaultsSelector` exists: the 15 research
default parameters, unchanged (no tuning plan is approved). A selector's id is part of
the protocol fingerprint.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from swing_trading_bot.broker.models import BarFeed, DailyBar
from swing_trading_bot.domain.calendar import TradingSession
from swing_trading_bot.strategy.backtest import (
    BacktestConfig,
    BacktestResult,
    ExitPolicyMode,
    compare_policies,
)
from swing_trading_bot.strategy.evaluation import (
    METRICS_VERSION,
    MIN_COMPLETED_TRADES,
    SESSIONS_PER_YEAR,
    PerformanceReport,
    SampleStatus,
    evaluate,
)
from swing_trading_bot.strategy.market_data import (
    CorporateActions,
    CoverageWindow,
    DividendPolicy,
    SeriesPreparation,
    SessionCalendar,
    prepare_series,
)
from swing_trading_bot.strategy.support_resistance import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    SupportResistanceParams,
)

PROTOCOL_VERSION: Final = "2"


class WalkForwardError(ValueError):
    """An invalid period, fold design or protocol."""


class ResearchAdmissionError(WalkForwardError):
    """The inputs were not admitted as RESEARCH_READY for evaluation."""


class HoldoutAccessError(PermissionError):
    """The final holdout was requested without a valid release."""


def _fp(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def add_months(d: date, months: int) -> date:
    """``d`` moved by ``months`` calendar months, the day clamped to the month's end."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


# ===================================================================== periods


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise WalkForwardError(f"range end {self.end} precedes start {self.start}")

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True, slots=True)
class ResearchPeriods:
    development: DateRange
    validation: DateRange
    holdout: DateRange

    def __post_init__(self) -> None:
        if not self.development.end < self.validation.start:
            raise WalkForwardError("validation must start after development ends")
        if not self.validation.end < self.holdout.start:
            raise WalkForwardError("the holdout must start after validation ends")

    def canonical(self) -> dict[str, str]:
        return {f"{name}_{edge}": str(getattr(getattr(self, name), edge))
                for name in ("development", "validation", "holdout")
                for edge in ("start", "end")}  # fmt: skip


#: The Step 16.15 proposal (research defaults, not an approved protocol).
DEFAULT_PERIODS: Final = ResearchPeriods(
    development=DateRange(date(2021, 1, 1), date(2023, 12, 31)),
    validation=DateRange(date(2024, 1, 1), date(2024, 12, 31)),
    holdout=DateRange(date(2025, 1, 1), date(2026, 10, 31)),
)


class WindowScheme(StrEnum):
    EXPANDING = "EXPANDING"
    ROLLING = "ROLLING"


@dataclass(frozen=True, slots=True)
class FoldDesign:
    scheme: WindowScheme = WindowScheme.EXPANDING
    initial_train_months: int = 12
    test_months: int = 6
    step_months: int = 6
    rolling_train_months: int | None = None
    embargo_sessions: int = 5
    min_test_sessions: int = 20

    def __post_init__(self) -> None:
        for name in ("initial_train_months", "test_months", "step_months", "min_test_sessions"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise WalkForwardError(f"{name} must be a positive integer")
        if isinstance(self.embargo_sessions, bool) or self.embargo_sessions < 0:
            raise WalkForwardError("embargo_sessions must be a non-negative integer")
        if self.step_months < self.test_months:
            raise WalkForwardError("step_months < test_months would overlap test windows")
        if (self.scheme is WindowScheme.ROLLING) != (self.rolling_train_months is not None):
            raise WalkForwardError("rolling_train_months is required for, and only for, ROLLING")
        if self.rolling_train_months is not None and self.rolling_train_months < 1:
            raise WalkForwardError("rolling_train_months must be positive")


class FoldKind(StrEnum):
    WALK_FORWARD = "WALK_FORWARD"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    kind: FoldKind
    train_start: date
    train_end: date
    embargo: tuple[date, ...]
    test_start: date
    test_end: date
    train_sessions: int
    test_sessions: int
    history_sessions: int
    """Calendar sessions before ``test_start`` (decision history available to the test)."""

    @property
    def label(self) -> str:
        return f"{self.kind.value}-{self.index:02d}:{self.test_start}..{self.test_end}"


def _first_on_or_after(dates: Sequence[date], d: date) -> int | None:
    for i, x in enumerate(dates):
        if x >= d:
            return i
    return None


def _last_on_or_before(dates: Sequence[date], d: date) -> int | None:
    found: int | None = None
    for i, x in enumerate(dates):
        if x > d:
            break
        found = i
    return found


def _fold(dates: Sequence[date], design: FoldDesign, *, index: int, kind: FoldKind,
          floor: date, test_from: date, test_to: date) -> Fold | None:  # fmt: skip
    lo = _first_on_or_after(dates, test_from)
    hi = _last_on_or_before(dates, test_to)
    if lo is None or hi is None or hi < lo:
        return None
    train_hi = lo - design.embargo_sessions - 1
    if design.scheme is WindowScheme.ROLLING and design.rolling_train_months is not None:
        floor = max(floor, add_months(dates[lo], -design.rolling_train_months))
    train_lo = _first_on_or_after(dates, floor)
    if train_lo is None or train_hi < train_lo:
        raise WalkForwardError(f"fold {index}: no training sessions before {dates[lo]}")
    return Fold(
        index=index, kind=kind, train_start=dates[train_lo], train_end=dates[train_hi],
        embargo=tuple(dates[train_hi + 1 : lo]), test_start=dates[lo], test_end=dates[hi],
        train_sessions=train_hi - train_lo + 1, test_sessions=hi - lo + 1, history_sessions=lo,
    )  # fmt: skip


def build_folds(
    periods: ResearchPeriods, design: FoldDesign, sessions: Sequence[date]
) -> tuple[Fold, ...]:
    """Walk-forward folds inside development, then the VALIDATION fold. Never the
    holdout. Deterministic for identical inputs."""
    dates = tuple(sessions)
    if any(b <= a for a, b in itertools.pairwise(dates)):
        raise WalkForwardError("sessions must be strictly increasing")
    dev = periods.development
    folds: list[Fold] = []
    test_from = add_months(dev.start, design.initial_train_months)
    while test_from <= dev.end:
        test_to = min(add_months(test_from, design.test_months) - timedelta(days=1), dev.end)
        fold = _fold(dates, design, index=len(folds), kind=FoldKind.WALK_FORWARD,
                     floor=dev.start, test_from=test_from, test_to=test_to)  # fmt: skip
        if fold is not None:
            folds.append(fold)
        test_from = add_months(test_from, design.step_months)
    val = _fold(dates, design, index=len(folds), kind=FoldKind.VALIDATION, floor=dev.start,
                test_from=periods.validation.start, test_to=periods.validation.end)  # fmt: skip
    if val is not None:
        folds.append(val)
    for f in folds:
        if f.test_end >= periods.holdout.start or f.train_end >= periods.holdout.start:
            raise WalkForwardError(f"{f.label} reaches the holdout")  # pragma: no cover
    return tuple(folds)


def holdout_fold(periods: ResearchPeriods, design: FoldDesign, sessions: Sequence[date]) -> Fold:
    """The final evaluation fold: training on development + validation, testing on the
    holdout. Defining it is harmless; evaluating it needs a release."""
    fold = _fold(tuple(sessions), design, index=0, kind=FoldKind.HOLDOUT,
                 floor=periods.development.start, test_from=periods.holdout.start,
                 test_to=periods.holdout.end)  # fmt: skip
    if fold is None:
        raise WalkForwardError("the calendar has no holdout sessions")
    return fold


def fold_table(folds: Sequence[Fold]) -> tuple[dict[str, object], ...]:
    return tuple({"label": f.label, **{k: (str(v) if isinstance(v, date) else v)
                                        for k, v in asdict(f).items() if k != "embargo"},
                  "embargo": [str(d) for d in f.embargo]} for f in folds)  # fmt: skip


# ===================================================================== sealed data


RESEARCH_READY: Final = "RESEARCH_READY"


@dataclass(frozen=True, slots=True)
class ResearchAdmission:
    """The research layer's statement that a dataset passed both readiness gates
    (acquisition complete and research-ready). Issued by
    ``ResearchDataset.research_inputs``; never inferred."""

    dataset_digest: str
    state: str
    readiness_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResearchInputs:
    """An integrity-checked dataset in memory. ``corporate_actions[symbol]`` is None when
    the dataset could not establish the record (the series is then rejected)."""

    dataset_digest: str
    sessions: tuple[TradingSession, ...]
    bars: Mapping[str, tuple[DailyBar, ...]]
    corporate_actions: Mapping[str, CorporateActions | None]
    sectors: Mapping[str, str] = field(default_factory=dict)
    feed: BarFeed = "sip"
    dividend_policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE
    file_checksums: Mapping[str, str] = field(default_factory=dict)
    """Dataset file -> SHA-256 (from the verified manifest); part of the frozen protocol."""
    universe_id: str = ""
    universe_fingerprint: str = ""
    admission: ResearchAdmission | None = None
    """Required by :class:`SealedResearchData` (no evaluation without it)."""

    def __post_init__(self) -> None:
        if set(self.bars) != set(self.corporate_actions):
            raise WalkForwardError("bars and corporate actions must name the same symbols")
        if not self.sessions:
            raise WalkForwardError("no calendar sessions")
        if not self.dataset_digest:
            raise WalkForwardError("a dataset digest is required")


@dataclass(frozen=True, slots=True)
class HoldoutRelease:
    """The explicit, recorded decision to evaluate the holdout once, for one frozen
    protocol (:class:`FrozenProtocol`) whose walk-forward and validation report it names."""

    dataset_digest: str
    frozen_protocol_fingerprint: str
    walk_forward_report_fingerprint: str
    approved_by: str
    reason: str
    released_on: date

    def __post_init__(self) -> None:
        if not self.approved_by.strip() or not self.reason.strip():
            raise WalkForwardError("a holdout release needs an approver and a reason")

    @property
    def fingerprint(self) -> str:
        return _fp({k: str(v) for k, v in asdict(self).items()})


@dataclass(frozen=True, slots=True)
class DataView:
    """Everything known at the close of ``until``: nothing later exists in it."""

    until: date
    dataset_digest: str
    calendar: SessionCalendar
    preparations: tuple[SeriesPreparation, ...]
    sectors: Mapping[str, str]
    includes_holdout: bool

    def history_before(self, d: date) -> int:
        return sum(1 for x in self.calendar.dates if x < d)


def _clip_window(w: CoverageWindow | None, until: date) -> CoverageWindow | None:
    if w is None or w.start > until:
        return None
    return CoverageWindow(w.start, min(w.end, until), w.source)


def clip_corporate_actions(ca: CorporateActions | None, until: date) -> CorporateActions | None:
    """The record as known at ``until``: later events removed, coverage clipped."""
    if ca is None:
        return None
    return CorporateActions(
        ca.symbol,
        tuple(s for s in ca.splits if s.effective_session <= until),
        tuple(d for d in ca.dividends if d.ex_session <= until),
        _clip_window(ca.split_coverage, until),
        _clip_window(ca.dividend_coverage, until),
    )


class SealedResearchData:
    """Research inputs with the holdout sealed. The only way to read them is
    :meth:`view`."""

    __slots__ = ("_holdout_start", "_inputs", "_readiness")

    def __init__(self, inputs: ResearchInputs, *, holdout_start: date) -> None:
        admission = inputs.admission
        if not isinstance(admission, ResearchAdmission):
            raise ResearchAdmissionError("the inputs carry no research admission")
        if admission.state != RESEARCH_READY:
            raise ResearchAdmissionError(f"the dataset is {admission.state}, not RESEARCH_READY")
        if admission.dataset_digest != inputs.dataset_digest:
            raise ResearchAdmissionError("the admission is for a different dataset")
        if not admission.readiness_fingerprint:
            raise ResearchAdmissionError("the admission names no readiness fingerprint")
        self._inputs = inputs
        self._readiness = admission.readiness_fingerprint
        self._holdout_start = holdout_start

    @property
    def dataset_digest(self) -> str:
        return self._inputs.dataset_digest

    @property
    def holdout_start(self) -> date:
        return self._holdout_start

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Calendar dates only (fold construction); no prices."""
        return tuple(s.session_date for s in self._inputs.sessions)

    @property
    def sectors(self) -> Mapping[str, str]:
        return dict(self._inputs.sectors)

    @property
    def identity(self) -> dict[str, object]:
        """Dataset and universe identity (no prices)."""
        i = self._inputs
        return {"dataset_digest": i.dataset_digest,
                "readiness_fingerprint": self._readiness,
                "file_checksums": dict(sorted(i.file_checksums.items())),
                "universe_id": i.universe_id, "universe_fingerprint": i.universe_fingerprint,
                "symbols": sorted(i.bars), "feed": i.feed,
                "dividend_policy": i.dividend_policy.value}  # fmt: skip

    def view(self, until: date, *, release: HoldoutRelease | None = None) -> DataView:
        inputs = self._inputs
        reaches = until >= self._holdout_start
        if reaches:
            if not isinstance(release, HoldoutRelease):
                raise HoldoutAccessError(
                    f"{until} is in the sealed holdout (from {self._holdout_start})"
                )
            if release.dataset_digest != inputs.dataset_digest:
                raise HoldoutAccessError("the release is for a different dataset")
        sessions = [s for s in inputs.sessions if s.session_date <= until]
        if not sessions:
            raise WalkForwardError(f"no sessions on or before {until}")
        calendar = SessionCalendar(sessions)
        preps = tuple(
            prepare_series(sym, [b for b in inputs.bars[sym] if b.session_date <= until],
                           calendar=calendar,
                           corporate_actions=clip_corporate_actions(
                               inputs.corporate_actions[sym], until),
                           dividend_policy=inputs.dividend_policy, feed=inputs.feed)
            for sym in sorted(inputs.bars)
        )  # fmt: skip
        return DataView(until, inputs.dataset_digest, calendar, preps, dict(inputs.sectors),
                        includes_holdout=reaches)  # fmt: skip


# ===================================================================== selection


class ParameterSelector(Protocol):
    @property
    def selector_id(self) -> str: ...

    def select(
        self, training: DataView, defaults: SupportResistanceParams
    ) -> SupportResistanceParams: ...


class FixedDefaultsSelector:
    """No tuning: the research defaults, whatever the training data. (Approving a tuning
    plan is a separate decision; any tuned selector must accept only a training view.)"""

    @property
    def selector_id(self) -> str:
        return "fixed-defaults-v1"

    def select(
        self, training: DataView, defaults: SupportResistanceParams
    ) -> SupportResistanceParams:
        if training.includes_holdout:  # defence in depth: never select on the holdout
            raise HoldoutAccessError("parameter selection was given holdout data")
        return defaults


# ===================================================================== protocol


class PortfolioMode(StrEnum):
    INDEPENDENT_FOLDS = "INDEPENDENT_FOLDS"
    """Each test window starts flat with ``initial_equity`` cash, nothing pending, no
    receivables; whatever it ends with (open positions, unsettled proceeds, receivables)
    is reported for that fold and discarded. Fold returns are never chained."""
    CONTINUOUS = "CONTINUOUS"
    """One portfolio carried chronologically across consecutive test windows. Supported
    only where a single replay represents it exactly (:func:`run_continuous`)."""


class UnsupportedEvaluationModeError(WalkForwardError):
    """The requested evaluation cannot be represented accurately by the backtester."""


#: The backtest semantics the protocol was written against (A§12.45-§12.46, §12.48).
ENGINE_VERSION: Final = "backtest-16.15A"

RELEASE_CONDITIONS: Final = (
    "the walk-forward and validation report was produced under this exact frozen protocol "
    "and its fingerprint is named in the release",
    "no change since that report to the dataset, universe, strategy or engine version, "
    "parameters, selection procedure, execution policy, costs, folds or metrics "
    "(any change produces a new frozen fingerprint and voids the release)",
    "a named approver and a recorded reason",
    "parameter selection for the holdout fold uses only data before its embargo",
    "one holdout evaluation per release",
)


@dataclass(frozen=True, slots=True)
class WalkForwardProtocol:
    periods: ResearchPeriods
    design: FoldDesign
    template: BacktestConfig
    """Backtest settings; ``start``/``end`` are replaced per fold."""
    selector_id: str
    min_completed_trades: int = MIN_COMPLETED_TRADES
    portfolio_mode: PortfolioMode = PortfolioMode.INDEPENDENT_FOLDS


@dataclass(frozen=True, slots=True)
class FrozenProtocol:
    """Everything that determines a research result, fixed before the holdout is
    released. Any field change changes :attr:`fingerprint`."""

    identity: Mapping[str, object]
    """Dataset digest and per-file checksums, universe id/fingerprint, symbols, feed."""
    strategy: Mapping[str, str]
    """Strategy id and version, engine version, protocol version."""
    parameters: Mapping[str, object]
    parameters_fingerprint: str
    execution: Mapping[str, object]
    """Backtest template minus per-fold dates: exit template, assumptions (fees,
    slippage, entry timing, final-price policy), risk limits, capital."""
    policies_reported: tuple[str, ...]
    portfolio_mode: str
    folds: tuple[Mapping[str, object], ...]
    """Every walk-forward, validation and holdout fold, resolved on the dataset calendar."""
    periods: Mapping[str, str]
    design: Mapping[str, str]
    metrics: Mapping[str, object]
    selection: Mapping[str, str]
    release_conditions: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return _fp(asdict(self))


def freeze_protocol(sealed: SealedResearchData, protocol: WalkForwardProtocol) -> FrozenProtocol:
    cfg = protocol.template.canonical()
    for key in ("start", "end", "params", "policy"):
        cfg.pop(key, None)
    dates = sealed.session_dates
    folds = (*build_folds(protocol.periods, protocol.design, dates),
             holdout_fold(protocol.periods, protocol.design, dates))  # fmt: skip
    params = protocol.template.params
    return FrozenProtocol(
        identity=sealed.identity,
        strategy={"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION,
                  "engine_version": ENGINE_VERSION, "protocol_version": PROTOCOL_VERSION},
        parameters=dict(params.canonical()), parameters_fingerprint=params.fingerprint,
        execution=cfg,
        policies_reported=(ExitPolicyMode.PROTECTION_FIRST.value,
                           ExitPolicyMode.HYPOTHETICAL_LINKED_EXITS.value),
        portfolio_mode=protocol.portfolio_mode.value,
        folds=fold_table(folds), periods=protocol.periods.canonical(),
        design={k: str(v) for k, v in asdict(protocol.design).items()},
        metrics={"metrics_version": METRICS_VERSION,
                 "min_completed_trades": protocol.min_completed_trades,
                 "sessions_per_year": SESSIONS_PER_YEAR},
        selection={"selector_id": protocol.selector_id,
                   "data": "training view ending before each fold's embargo; never the holdout"},
        release_conditions=RELEASE_CONDITIONS,
    )  # fmt: skip


class FoldStatus(StrEnum):
    EVALUATED = "EVALUATED"
    INSUFFICIENT_WARMUP = "INSUFFICIENT_WARMUP"
    INSUFFICIENT_TEST_SESSIONS = "INSUFFICIENT_TEST_SESSIONS"


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: Fold
    status: FoldStatus
    params_fingerprint: str
    selection_data_until: date
    test_data_until: date
    executable: PerformanceReport | None
    """PROTECTION_FIRST (executable through the current OMS)."""
    hypothetical: PerformanceReport | None
    """HYPOTHETICAL_LINKED_EXITS (not executable; research comparison only)."""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PolicyAggregate:
    """Counts across folds. Returns are deliberately absent: independent folds each
    restart with fresh capital, so their returns cannot be chained into one track record."""

    policy: str
    evaluated_folds: int
    completed_trades: int
    open_trades_at_fold_ends: int
    sample_status: SampleStatus


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    protocol_fingerprint: str
    """The :class:`FrozenProtocol` fingerprint."""
    dataset_digest: str
    selector_id: str
    portfolio_mode: PortfolioMode
    folds: tuple[FoldResult, ...]
    aggregates: tuple[PolicyAggregate, ...]
    holdout_evaluated: bool
    holdout_release: str | None
    report_fingerprint: str


def _aggregate(results: Sequence[FoldResult], minimum: int) -> tuple[PolicyAggregate, ...]:
    out: list[PolicyAggregate] = []
    for attr in ("executable", "hypothetical"):
        reports = [getattr(r, attr) for r in results if getattr(r, attr) is not None]
        completed = sum(rep.completed_trades for rep in reports)
        status = (SampleStatus.NO_COMPLETED_TRADES if completed == 0 else
                  SampleStatus.INSUFFICIENT if completed < minimum else
                  SampleStatus.SUFFICIENT)  # fmt: skip
        policy = reports[0].policy if reports else attr.upper()
        out.append(PolicyAggregate(policy, len(reports), completed,
                                   sum(rep.open_trades for rep in reports), status))  # fmt: skip
    return tuple(out)


def _select(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    selector: ParameterSelector,
    fold: Fold,
) -> tuple[DataView, SupportResistanceParams]:
    training = sealed.view(fold.train_end)  # always before the fold's embargo
    if training.includes_holdout or training.until >= sealed.holdout_start:
        raise HoldoutAccessError("parameter selection would see the holdout")  # pragma: no cover
    params = selector.select(training, protocol.template.params)
    if not isinstance(params, SupportResistanceParams):
        raise WalkForwardError("the selector must return SupportResistanceParams")
    return training, params


def _evaluate_fold(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    selector: ParameterSelector,
    fold: Fold,
    release: HoldoutRelease | None,
) -> FoldResult:
    training, params = _select(sealed, protocol, selector, fold)
    test = sealed.view(fold.test_end, release=release)

    def result(
        status: FoldStatus,
        executable: PerformanceReport | None = None,
        hypothetical: PerformanceReport | None = None,
        detail: str = "",
    ) -> FoldResult:
        return FoldResult(fold, status, params.fingerprint, training.until, test.until,
                          executable, hypothetical, detail)  # fmt: skip

    minimum_test = protocol.design.min_test_sessions
    if fold.test_sessions < minimum_test:
        detail = f"{fold.test_sessions} < {minimum_test}"
        return result(FoldStatus.INSUFFICIENT_TEST_SESSIONS, detail=detail)
    history = test.history_before(fold.test_start)
    if history < params.required_sessions:
        return result(FoldStatus.INSUFFICIENT_WARMUP,
                      detail=f"{history} < {params.required_sessions} sessions")  # fmt: skip
    # A fresh portfolio: the template's initial equity, no positions, orders or receivables.
    cfg = replace(protocol.template, params=params, start=fold.test_start, end=fold.test_end)
    executable, hypothetical = compare_policies(test.preparations, test.calendar, cfg)
    minimum = protocol.min_completed_trades
    return result(
        FoldStatus.EVALUATED,
        evaluate(executable, test.calendar, sectors=test.sectors, min_completed_trades=minimum),
        evaluate(hypothetical, test.calendar, sectors=test.sectors, min_completed_trades=minimum),
    )


def _check(sealed: SealedResearchData, protocol: WalkForwardProtocol,
           selector: ParameterSelector) -> None:  # fmt: skip
    if selector.selector_id != protocol.selector_id:
        raise WalkForwardError(
            f"selector {selector.selector_id!r} is not the protocol's {protocol.selector_id!r}"
        )
    if sealed.holdout_start != protocol.periods.holdout.start:
        raise WalkForwardError("the sealed data and the protocol disagree on the holdout")


def _report(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    results: Sequence[FoldResult],
    release: HoldoutRelease | None,
) -> WalkForwardReport:
    pfp = freeze_protocol(sealed, protocol).fingerprint
    aggregates = _aggregate(results, protocol.min_completed_trades)
    body = {
        "protocol": pfp,
        "folds": [[fold_table([r.fold])[0], r.status.value, r.params_fingerprint, r.detail,
                   r.executable.fingerprint if r.executable else None,
                   r.hypothetical.fingerprint if r.hypothetical else None] for r in results],
        "aggregates": [asdict(a) for a in aggregates],
        "release": release.fingerprint if release else None,
    }  # fmt: skip
    return WalkForwardReport(
        protocol_fingerprint=pfp, dataset_digest=sealed.dataset_digest,
        selector_id=protocol.selector_id, portfolio_mode=protocol.portfolio_mode,
        folds=tuple(results), aggregates=aggregates, holdout_evaluated=release is not None,
        holdout_release=release.fingerprint if release else None, report_fingerprint=_fp(body),
    )  # fmt: skip


def run_walk_forward(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    selector: ParameterSelector,
) -> WalkForwardReport:
    """Every WALK_FORWARD and VALIDATION fold as INDEPENDENT_FOLDS. Never touches the
    holdout. A CONTINUOUS protocol is refused here (use :func:`run_continuous`)."""
    _check(sealed, protocol, selector)
    if protocol.portfolio_mode is not PortfolioMode.INDEPENDENT_FOLDS:
        raise UnsupportedEvaluationModeError(
            "run_walk_forward evaluates independent folds only; it never stitches fold "
            "results into a continuous portfolio"
        )
    folds = build_folds(protocol.periods, protocol.design, sealed.session_dates)
    results = [_evaluate_fold(sealed, protocol, selector, f, None) for f in folds]
    return _report(sealed, protocol, results, None)


def run_holdout(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    selector: ParameterSelector,
    release: HoldoutRelease,
) -> WalkForwardReport:
    """The final, released evaluation of the frozen protocol on the holdout (independent
    fold). The release must name the current frozen fingerprint and the fingerprint of the
    walk-forward report that this protocol reproduces now."""
    _check(sealed, protocol, selector)
    if protocol.portfolio_mode is not PortfolioMode.INDEPENDENT_FOLDS:
        raise UnsupportedEvaluationModeError("the holdout is evaluated as an independent fold")
    if release.dataset_digest != sealed.dataset_digest:
        raise HoldoutAccessError("the release is for a different dataset")
    if release.frozen_protocol_fingerprint != freeze_protocol(sealed, protocol).fingerprint:
        raise HoldoutAccessError("the protocol changed since the release (or never matched)")
    reproduced = run_walk_forward(sealed, protocol, selector)
    if reproduced.report_fingerprint != release.walk_forward_report_fingerprint:
        raise HoldoutAccessError("the release does not name this protocol's validation report")
    fold = holdout_fold(protocol.periods, protocol.design, sealed.session_dates)
    return _report(sealed, protocol, [_evaluate_fold(sealed, protocol, selector, fold, release)],
                   release)  # fmt: skip


# ===================================================================== continuous


@dataclass(frozen=True, slots=True)
class BoundaryState:
    """The carried portfolio at the close of a fold's last test session."""

    session: date
    cash: Decimal
    market_value: Decimal
    dividends_receivable: Decimal
    equity: Decimal
    open_trade_ids: tuple[str, ...]
    """Trades entered on or before ``session`` and not completed by it."""


@dataclass(frozen=True, slots=True)
class ContinuousReport:
    protocol_fingerprint: str
    dataset_digest: str
    start: date
    end: date
    params_fingerprint: str
    executable: PerformanceReport
    hypothetical: PerformanceReport
    boundaries_executable: tuple[BoundaryState, ...]
    boundaries_hypothetical: tuple[BoundaryState, ...]
    report_fingerprint: str


def _boundaries(result: BacktestResult, ends: Sequence[date]) -> tuple[BoundaryState, ...]:
    by_session = {p.session: p for p in result.equity_curve}
    out: list[BoundaryState] = []
    for end in ends:
        p = by_session[end]
        carried = tuple(t.trade_id for t in result.trades if t.entry_session <= end and (
            t.exit_session is None or t.exit_session > end))  # fmt: skip
        out.append(BoundaryState(end, p.cash, p.market_value, p.dividends_receivable, p.equity,
                                 carried))  # fmt: skip
    return tuple(out)


def run_continuous(
    sealed: SealedResearchData,
    protocol: WalkForwardProtocol,
    selector: ParameterSelector,
) -> ContinuousReport:
    """One portfolio across every walk-forward and validation test window, as ONE replay.

    Exact only when the selected parameters are identical in every fold and the test
    windows are calendar-contiguous; otherwise a single replay could not reproduce what
    a carried portfolio under per-fold parameters would do, so the mode is refused
    (:class:`UnsupportedEvaluationModeError`) rather than approximated. Positions,
    pending orders, cash, unsettled proceeds and receivables cross fold boundaries inside
    the replay; per-boundary snapshots are reported. Never touches the holdout."""
    _check(sealed, protocol, selector)
    if protocol.portfolio_mode is not PortfolioMode.CONTINUOUS:
        raise UnsupportedEvaluationModeError("the protocol is not CONTINUOUS")
    dates = sealed.session_dates
    folds = build_folds(protocol.periods, protocol.design, dates)
    if not folds:
        raise UnsupportedEvaluationModeError("no folds")
    chosen = [_select(sealed, protocol, selector, f)[1] for f in folds]
    if len({p.fingerprint for p in chosen}) != 1:
        raise UnsupportedEvaluationModeError(
            "parameters differ between folds: a single replay cannot represent a carried "
            "portfolio whose rules change at fold boundaries"
        )
    index = {d: i for i, d in enumerate(dates)}
    for a, b in itertools.pairwise(folds):
        if index[b.test_start] != index[a.test_end] + 1:
            raise UnsupportedEvaluationModeError(
                f"test windows are not contiguous ({a.test_end} -> {b.test_start})"
            )
    params = chosen[0]
    if folds[0].history_sessions < params.required_sessions:
        raise UnsupportedEvaluationModeError("insufficient warm-up before the first test window")
    end = folds[-1].test_end
    view = sealed.view(end)
    cfg = replace(protocol.template, params=params, start=folds[0].test_start, end=end)
    executable, hypothetical = compare_policies(view.preparations, view.calendar, cfg)
    minimum = protocol.min_completed_trades
    ends = [f.test_end for f in folds]
    rep_e = evaluate(executable, view.calendar, sectors=view.sectors, min_completed_trades=minimum)
    rep_h = evaluate(hypothetical, view.calendar, sectors=view.sectors,
                     min_completed_trades=minimum)  # fmt: skip
    pfp = freeze_protocol(sealed, protocol).fingerprint
    return ContinuousReport(
        protocol_fingerprint=pfp, dataset_digest=sealed.dataset_digest,
        start=folds[0].test_start, end=end, params_fingerprint=params.fingerprint,
        executable=rep_e, hypothetical=rep_h,
        boundaries_executable=_boundaries(executable, ends),
        boundaries_hypothetical=_boundaries(hypothetical, ends),
        report_fingerprint=_fp([pfp, rep_e.fingerprint, rep_h.fingerprint]),
    )  # fmt: skip


__all__ = [
    "DEFAULT_PERIODS",
    "ENGINE_VERSION",
    "PROTOCOL_VERSION",
    "RELEASE_CONDITIONS",
    "RESEARCH_READY",
    "BoundaryState",
    "ContinuousReport",
    "DataView",
    "DateRange",
    "FixedDefaultsSelector",
    "Fold",
    "FoldDesign",
    "FoldKind",
    "FoldResult",
    "FoldStatus",
    "FrozenProtocol",
    "HoldoutAccessError",
    "HoldoutRelease",
    "ParameterSelector",
    "PolicyAggregate",
    "PortfolioMode",
    "ResearchAdmission",
    "ResearchAdmissionError",
    "ResearchInputs",
    "ResearchPeriods",
    "SealedResearchData",
    "UnsupportedEvaluationModeError",
    "WalkForwardError",
    "WalkForwardProtocol",
    "WalkForwardReport",
    "WindowScheme",
    "add_months",
    "build_folds",
    "clip_corporate_actions",
    "fold_table",
    "freeze_protocol",
    "holdout_fold",
    "run_continuous",
    "run_holdout",
    "run_walk_forward",
]
