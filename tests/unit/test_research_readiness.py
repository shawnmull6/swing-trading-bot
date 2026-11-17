"""ACQUISITION_COMPLETE vs RESEARCH_READY, and the evaluation entry points that refuse
anything not research-ready (Step 16.16E.1, Task 3). SYNTHETIC fixtures only: no
network, credential or ledger is touched."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.research.dataset import (
    ACQUISITION_COMPLETE,
    CoverageAcceptance,
    DatasetNotResearchReadyError,
    DatasetState,
    EvidenceKind,
    ResearchDataset,
    assess_readiness,
    load_dataset,
    quality_report,
    write_dataset,
)
from swing_trading_bot.strategy.backtest import BacktestConfig, ExecutionAssumptions, ExitPolicyMode
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from swing_trading_bot.strategy.walk_forward import (
    DEFAULT_PERIODS,
    RESEARCH_READY,
    FoldDesign,
    HoldoutAccessError,
    HoldoutRelease,
    ResearchAdmission,
    ResearchAdmissionError,
    ResearchInputs,
    SealedResearchData,
    WalkForwardProtocol,
    freeze_protocol,
)
from tests.support.research_fixture import (
    BATCH,
    CAL_BATCH,
    FixtureSpec,
    acquisition_report,
    build_files,
    default_spec,
    full_coverage,
    record,
)
from tests.support.strategy_fixture import LIMITS, ZERO_FEES

HOLDOUT = DEFAULT_PERIODS.holdout.start


def _load(tmp_path: Path, spec: FixtureSpec | None = None, *,
          report: object = "default", acceptance: CoverageAcceptance | None = None,
          name: str = "ds") -> ResearchDataset:  # fmt: skip
    meta, files = build_files(spec or default_spec())
    if report is None:
        meta.pop("acquisition_report")
    elif report != "default":
        meta["acquisition_report"] = report
    write_dataset(tmp_path / name, meta, files)
    return load_dataset(tmp_path / name, acceptance=acceptance)


def _protocol() -> WalkForwardProtocol:
    periods = DEFAULT_PERIODS
    template = BacktestConfig(SupportResistanceParams(), ExitTemplate(),
                              ExitPolicyMode.PROTECTION_FIRST,
                              ExecutionAssumptions(fees=ZERO_FEES), LIMITS,
                              periods.development.start, periods.development.start)  # fmt: skip
    return WalkForwardProtocol(periods, FoldDesign(), template, "fixed-defaults-v1")


# ===================================================================== the two gates


def test_a_complete_attested_dataset_is_research_ready(tmp_path: Path) -> None:
    ds = _load(tmp_path)
    r = assess_readiness(ds)
    assert r.state is DatasetState.RESEARCH_READY
    assert r.acquisition_complete and r.research_ready
    assert r.acquisition_blockers == () and r.research_blockers == ()
    inputs = ds.research_inputs()
    assert inputs.admission == ResearchAdmission(ds.digest, RESEARCH_READY, r.fingerprint)
    assert quality_report(ds)["readiness"]["state"] == "RESEARCH_READY"


@pytest.mark.parametrize(
    ("report", "blocker"),
    [
        (None, "ACQUISITION_REPORT_MISSING"),
        (acquisition_report(state="PARTIAL"), "ACQUISITION_STATE 'PARTIAL'"),
        ({"acquisition_state": ACQUISITION_COMPLETE}, "REQUIRED_BATCHES_MISSING"),
        (acquisition_report(batches=(BATCH, CAL_BATCH, "never-fetched")),
         "REQUIRED_BATCH_ABSENT never-fetched"),
        (acquisition_report(batches=(BATCH,)), f"UNREQUIRED_BATCH {CAL_BATCH}"),
        ({"acquisition_state": ACQUISITION_COMPLETE,
          "required_batches": {BATCH: {"pages_sha256": ["not-a-digest"]},
                               CAL_BATCH: {"pages_sha256": []}}},
         f"REQUIRED_BATCH_UNCHECKSUMMED {BATCH}"),
    ],
)  # fmt: skip
def test_acquisition_incomplete_blocks_everything_but_inspection(
    tmp_path: Path, report: object, blocker: str
) -> None:
    ds = _load(tmp_path, report=report)
    r = ds.readiness()
    assert r.state is DatasetState.ACQUISITION_INCOMPLETE
    assert blocker in r.acquisition_blockers
    assert not r.research_ready and r.research_blockers[0] == "ACQUISITION_INCOMPLETE"
    with pytest.raises(DatasetNotResearchReadyError, match="ACQUISITION_INCOMPLETE"):
        ds.research_inputs()
    q = quality_report(ds)  # still fully inspectable
    assert q["readiness"]["state"] == "ACQUISITION_INCOMPLETE"
    assert {row["symbol"] for row in q["symbols"]} == {"AAA", "BBB", "CCC"}


def test_complete_but_a_rejected_symbol_is_not_research_ready(tmp_path: Path) -> None:
    spec = default_spec()
    spec.records["CCC"] = None
    ds = _load(tmp_path, spec)
    r = ds.readiness()
    assert r.acquisition_complete and not r.research_ready
    assert r.state is DatasetState.ACQUISITION_COMPLETE
    assert any(b.startswith("SYMBOL_REJECTED CCC") for b in r.research_blockers)
    with pytest.raises(DatasetNotResearchReadyError) as err:
        ds.research_inputs()
    assert err.value.readiness == r
    q = quality_report(ds)  # the rejection stays visible with its reasons
    assert "CCC" in q["rejected"] and q["readiness"]["research_ready"] is False


def test_provider_only_coverage_is_never_research_ready(tmp_path: Path) -> None:
    """Even an explicit policy accepting PROVIDER_REPORTED (symbols load as accepted with
    a warning) does not make the dataset research-ready: attestations are required."""
    reported = full_coverage(EvidenceKind.PROVIDER_REPORTED)
    spec = default_spec()
    spec.records = {s: record(s, split_cov=reported, div_cov=reported, other_cov=reported)
                    for s in spec.records}  # fmt: skip
    lenient = CoverageAcceptance(*(frozenset({EvidenceKind.PROVIDER_REPORTED}),) * 3)
    ds = _load(tmp_path, spec, acceptance=lenient)
    assert ds.accepted_symbols() == ("AAA", "BBB", "CCC")
    r = ds.readiness()
    assert r.state is DatasetState.ACQUISITION_COMPLETE
    assert "PROVIDER_ONLY_COVERAGE AAA" in r.research_blockers
    assert "ATTESTATION_MISSING AAA dividends" in r.research_blockers
    with pytest.raises(DatasetNotResearchReadyError):
        ds.research_inputs()


def test_an_attestation_must_span_the_whole_dataset_window(tmp_path: Path) -> None:
    short = full_coverage(start=date(2021, 1, 1))  # attested, but not from warm-up start
    spec = default_spec()
    spec.records["BBB"] = record("BBB", split_cov=short)
    r = _load(tmp_path, spec).readiness()
    assert "ATTESTATION_MISSING BBB splits" in r.research_blockers
    assert not r.research_ready


# ===================================================================== entry points


def _ready_inputs(tmp_path: Path) -> ResearchInputs:
    return _load(tmp_path).research_inputs()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"admission": None}, "no research admission"),
        ({"state": "ACQUISITION_COMPLETE"}, "ACQUISITION_COMPLETE, not RESEARCH_READY"),
        ({"state": "ACQUISITION_INCOMPLETE"}, "not RESEARCH_READY"),
        ({"dataset_digest": "f" * 64}, "different dataset"),
        ({"readiness_fingerprint": ""}, "no readiness fingerprint"),
    ],
)
def test_evaluation_refuses_inputs_not_admitted_as_research_ready(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    inputs = _ready_inputs(tmp_path)
    assert inputs.admission is not None
    if "admission" in change:
        bad = replace(inputs, admission=None)
    else:
        bad = replace(inputs, admission=replace(inputs.admission, **change))
    with pytest.raises(ResearchAdmissionError, match=message):
        SealedResearchData(bad, holdout_start=HOLDOUT)  # every entry point needs one


def test_inputs_built_by_hand_are_not_admitted(tmp_path: Path) -> None:
    inputs = _ready_inputs(tmp_path)
    raw = ResearchInputs(inputs.dataset_digest, inputs.sessions, inputs.bars,
                         inputs.corporate_actions)  # fmt: skip
    with pytest.raises(ResearchAdmissionError):
        SealedResearchData(raw, holdout_start=HOLDOUT)


# ===================================================================== holdout


def test_a_research_ready_dataset_still_keeps_the_holdout_sealed(tmp_path: Path) -> None:
    sealed = SealedResearchData(_ready_inputs(tmp_path), holdout_start=HOLDOUT)
    sealed.view(date(2024, 12, 31))
    with pytest.raises(HoldoutAccessError):
        sealed.view(HOLDOUT)
    other = HoldoutRelease("e" * 64, "f", "w", "operator", "reason", date(2026, 11, 16))
    with pytest.raises(HoldoutAccessError, match="different dataset"):
        sealed.view(HOLDOUT, release=other)


def test_the_frozen_protocol_binds_the_readiness_assessment(tmp_path: Path) -> None:
    inputs = _ready_inputs(tmp_path)
    assert inputs.admission is not None
    a = SealedResearchData(inputs, holdout_start=HOLDOUT)
    b = SealedResearchData(
        replace(inputs, admission=replace(inputs.admission, readiness_fingerprint="0" * 64)),
        holdout_start=HOLDOUT,
    )
    assert a.identity["readiness_fingerprint"] == inputs.admission.readiness_fingerprint
    protocol = _protocol()
    assert freeze_protocol(a, protocol).fingerprint != freeze_protocol(b, protocol).fingerprint


# ===================================================================== ledger


def test_readiness_and_admission_never_touch_a_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placeholder named like the ledger sits next to the dataset; nothing opens it and
    no SQLite connection is attempted anywhere on the readiness/admission path."""
    placeholder = tmp_path / "ledger.sqlite3"
    placeholder.write_bytes(b"")

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("sqlite3.connect called")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    ds = _load(tmp_path / "research")
    r = ds.readiness()
    json.dumps(quality_report(ds))
    SealedResearchData(ds.research_inputs(), holdout_start=HOLDOUT).view(date(2022, 1, 3))
    assert r.research_ready
    assert placeholder.read_bytes() == b""
