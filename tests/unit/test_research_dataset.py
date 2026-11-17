"""Versioned research dataset: integrity, provenance, corporate-action completeness and
quality assessment (Step 16.15, Phases 2, 3 and 6). All data is SYNTHETIC."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.research.dataset import (
    Coverage,
    CoverageAcceptance,
    DatasetError,
    DatasetFormatError,
    DatasetIntegrityError,
    Evidence,
    EvidenceKind,
    OtherEvent,
    OtherEventKind,
    QualityIssue,
    QualityStatus,
    ResearchDataset,
    TickerSpan,
    bars_from_alpaca_pages,
    canonical,
    encode_jsonl,
    load_dataset,
    loads,
    sha256,
    write_dataset,
)
from swing_trading_bot.strategy.backtest import (
    AuditKind,
    BacktestConfig,
    ExecutionAssumptions,
    ExitPolicyMode,
    run_backtest,
)
from swing_trading_bot.strategy.market_data import (
    CashDividend,
    SessionCalendar,
    SplitEvent,
    prepare_series,
)
from swing_trading_bot.strategy.signals import ExitTemplate
from swing_trading_bot.strategy.support_resistance import SupportResistanceParams
from tests.support.research_fixture import (
    DATES,
    SESSIONS,
    FixtureSpec,
    build_files,
    daily_bar,
    default_spec,
    full_coverage,
    identity,
    record,
    synthetic_bars,
    write_fixture,
)
from tests.support.strategy_fixture import LIMITS, ZERO_FEES

D = Decimal


def _load(tmp_path: Path, spec: FixtureSpec | None = None, **kw: object) -> ResearchDataset:
    root = tmp_path / "ds"
    write_fixture(root, spec)
    return load_dataset(root, **kw)  # type: ignore[arg-type]


def _quality(ds: ResearchDataset, symbol: str) -> tuple[QualityStatus, set[QualityIssue]]:
    q = next(q for q in ds.quality if q.symbol == symbol)
    return q.status, {f.issue for f in q.findings}


def _writable(root: Path) -> None:
    for p in root.rglob("*"):
        if p.is_file():
            p.chmod(0o644)


# ===================================================================== integrity


def test_round_trip_is_deterministic_and_read_only(tmp_path: Path) -> None:
    d1 = write_fixture(tmp_path / "a")
    d2 = write_fixture(tmp_path / "b")
    assert d1 == d2  # identical content => identical dataset identity
    ds = load_dataset(tmp_path / "a")
    assert ds.digest == d1
    assert ds.manifest.symbols == ("AAA", "BBB", "CCC")
    assert ds.manifest.survivorship_bias_free is False
    assert len(ds.sessions) == len(SESSIONS)
    assert ds.bars["AAA"] == synthetic_bars("AAA")
    assert all(not (p.stat().st_mode & 0o222) for p in (tmp_path / "a").rglob("*") if p.is_file())
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.ACCEPTED_WITH_WARNINGS
    assert issues == {QualityIssue.SPLIT_CONSISTENCY_CHECK_UNAVAILABLE}
    assert ds.accepted_symbols() == ("AAA", "BBB", "CCC")


def test_datasets_are_immutable(tmp_path: Path) -> None:
    write_fixture(tmp_path / "ds")
    with pytest.raises(DatasetError):
        write_fixture(tmp_path / "ds")
    meta, files = build_files(default_spec())
    with pytest.raises(DatasetError):
        write_dataset(tmp_path / "x", meta, {**files, "../escape.json": b"{}"})
    with pytest.raises(DatasetError):
        write_dataset(tmp_path / "y", {**meta, "manifest_sha256": "0"}, files)
    assert not (tmp_path / ".x.staging").exists()


@pytest.mark.parametrize("tamper", ["byte", "extra", "missing", "manifest", "schema"])
def test_any_tampering_is_refused(tmp_path: Path, tamper: str) -> None:
    root = tmp_path / "ds"
    write_fixture(root)
    _writable(root)
    bars = root / "bars" / "AAA.raw.jsonl"
    if tamper == "byte":
        blob = bars.read_bytes()
        bars.write_bytes(blob.replace(b'"c":"56', b'"c":"57', 1))
    elif tamper == "extra":
        (root / "bars" / "ZZZ.raw.jsonl").write_bytes(b"")
    elif tamper == "missing":
        bars.unlink()
    else:
        manifest = json.loads((root / "manifest.json").read_text())
        if tamper == "manifest":
            manifest["dataset_id"] = "renamed"
        else:
            manifest["schema_version"] = 1  # an older format is refused, not migrated
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DatasetIntegrityError):
        load_dataset(root)


@pytest.mark.parametrize("blob", ['{"o": 1.5}', '{"o": NaN}', '{"o": Infinity}', "{"])
def test_strict_json_refuses_floats_and_non_finite_numbers(blob: str) -> None:
    with pytest.raises(DatasetFormatError):
        loads(blob)


def test_a_float_price_row_rejects_the_symbol_without_repair(tmp_path: Path) -> None:
    meta, files = build_files(default_spec())
    rows = files["bars/AAA.raw.jsonl"].decode().splitlines()
    row = json.loads(rows[10])
    row["o"] = 50.25
    rows[10] = json.dumps(row)
    files["bars/AAA.raw.jsonl"] = ("\n".join(rows) + "\n").encode()
    write_dataset(tmp_path / "ds", meta, files)
    ds = load_dataset(tmp_path / "ds")
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.BAR_ROW_INVALID in issues
    assert ds.corporate_actions["AAA"] is None
    assert _quality(ds, "BBB")[0] is QualityStatus.ACCEPTED_WITH_WARNINGS


def test_calendar_must_carry_settlement_dates_and_cover_the_window(tmp_path: Path) -> None:
    meta, files = build_files(default_spec())
    cal = json.loads(files["calendar.json"])
    del cal["sessions"][5]["settlement_date"]
    write_dataset(tmp_path / "a", meta, {**files, "calendar.json": json.dumps(cal).encode()})
    with pytest.raises(DatasetFormatError):
        load_dataset(tmp_path / "a")
    cal = json.loads(files["calendar.json"])
    cal["end"] = "2026-06-30"
    write_dataset(tmp_path / "b", meta, {**files, "calendar.json": json.dumps(cal).encode()})
    with pytest.raises(DatasetFormatError):
        load_dataset(tmp_path / "b")


def test_unknown_batch_rejects_the_symbol(tmp_path: Path) -> None:
    meta, files = build_files(default_spec())
    files["bars/BBB.raw.jsonl"] = files["bars/BBB.raw.jsonl"].replace(
        b"fixture-batch-1", b"unrecorded-batch")  # fmt: skip
    write_dataset(tmp_path / "ds", meta, files)
    status, issues = _quality(load_dataset(tmp_path / "ds"), "BBB")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.UNKNOWN_BATCH in issues


# ===================================================================== corporate actions


def test_missing_record_rejects_the_symbol_and_the_backtest_audits_it(tmp_path: Path) -> None:
    spec = default_spec()
    spec.records["CCC"] = None
    ds = _load(tmp_path, spec)
    status, issues = _quality(ds, "CCC")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.CORPORATE_ACTIONS_MISSING in issues
    inputs = ds.research_inputs()
    assert inputs.corporate_actions["CCC"] is None
    calendar = SessionCalendar(ds.sessions)
    preps = [
        prepare_series(
            s, inputs.bars[s], calendar=calendar, corporate_actions=inputs.corporate_actions[s]
        )
        for s in inputs.bars
    ]
    cfg = BacktestConfig(SupportResistanceParams(), ExitTemplate(), ExitPolicyMode.PROTECTION_FIRST,
                         ExecutionAssumptions(fees=ZERO_FEES), LIMITS, date(2021, 3, 1),
                         date(2021, 6, 30))  # fmt: skip
    audit = run_backtest(preps, calendar, cfg).audit
    rejected = [e for e in audit if e.kind is AuditKind.SERIES_REJECTED]
    assert [e.symbol for e in rejected] == ["CCC"]
    assert "CORPORATE_ACTIONS_MISSING" in rejected[0].detail


@pytest.mark.parametrize("which", ["splits", "dividends", "other"])
def test_provider_only_coverage_is_not_accepted_by_default(tmp_path: Path, which: str) -> None:
    spec = default_spec()
    reported = full_coverage(EvidenceKind.PROVIDER_REPORTED)
    kw = {"splits": "split_cov", "dividends": "div_cov", "other": "other_cov"}[which]
    spec.records["AAA"] = record("AAA", **{kw: reported})  # type: ignore[arg-type]
    status, issues = _quality(_load(tmp_path, spec), "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.COVERAGE_EVIDENCE_INSUFFICIENT in issues
    allowed = frozenset({EvidenceKind.PROVIDER_REPORTED, EvidenceKind.OPERATOR_REVIEW})
    lenient = CoverageAcceptance(splits=allowed, dividends=allowed, other_events=allowed)
    ds = load_dataset(tmp_path / "ds", acceptance=lenient)
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.ACCEPTED_WITH_WARNINGS
    assert QualityIssue.PROVIDER_ONLY_COVERAGE_ACCEPTED in issues  # labelled, never silent


def test_coverage_gaps_fail_closed(tmp_path: Path) -> None:
    spec = default_spec()
    partial = replace(full_coverage(), start=date(2022, 1, 3))
    spec.records["AAA"] = record("AAA", div_cov=partial)
    spec.records["BBB"] = record("BBB", other_cov=partial)
    ds = _load(tmp_path, spec)
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.REJECTED and QualityIssue.SERIES_REJECTED in issues
    q = next(q for q in ds.quality if q.symbol == "AAA")
    assert any(f.startswith("DIVIDEND_COVERAGE_GAP") for f in q.series_findings)
    status, issues = _quality(ds, "BBB")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.OTHER_EVENT_COVERAGE_MISSING in issues


def test_unsupported_event_inside_the_bar_span_rejects_the_symbol(tmp_path: Path) -> None:
    spec = default_spec()
    merger = OtherEvent(OtherEventKind.MERGER, date(2023, 5, 1), "acquired", "fixture")
    spec.records["AAA"] = record("AAA", others=[merger])
    status, issues = _quality(_load(tmp_path, spec), "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.UNSUPPORTED_CORPORATE_ACTION in issues


def test_split_record_is_cross_checked_against_adjusted_bars(tmp_path: Path) -> None:
    split_day = date(2022, 6, 6)
    i = DATES.index(split_day)
    raw = list(synthetic_bars("AAA", scale=D(2)))
    after = synthetic_bars("AAA", scale=D(1))
    raw[i:] = after[i:]  # a real 2-for-1: raw prices halve from the split session
    adjusted = [daily_bar("AAA", b.session_date, (b.open, b.high, b.low, b.close),
                          adjustment="split") for b in after]  # fmt: skip
    split = SplitEvent("AAA", split_day, 2, 1, source="fixture")
    spec = default_spec()
    spec.bars["AAA"] = raw
    spec.split_bars["AAA"] = adjusted
    spec.records["AAA"] = record("AAA", splits=[split])
    ds = _load(tmp_path, spec)
    assert _quality(ds, "AAA") == (QualityStatus.ACCEPTED, set())
    ca = ds.corporate_actions["AAA"]
    assert ca is not None and ca.splits == (split,)
    spec.records["AAA"] = record("AAA")  # the record omits the split the bars show
    ds = load_dataset(_rewrite(tmp_path, spec))
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.SPLIT_DERIVATION_MISMATCH in issues


def test_unrecorded_split_like_drop_is_kept_but_flagged(tmp_path: Path) -> None:
    i = DATES.index(date(2022, 6, 6))
    raw = list(synthetic_bars("AAA", scale=D(2)))
    raw[i:] = synthetic_bars("AAA", scale=D(1))[i:]
    spec = default_spec()
    spec.bars["AAA"] = raw
    status, issues = _quality(_load(tmp_path, spec), "AAA")
    assert status is QualityStatus.ACCEPTED_WITH_WARNINGS  # a crash is not excluded
    assert QualityIssue.PRICE_DISCONTINUITY_UNEXPLAINED in issues


def test_dividends_are_loaded_and_booked(tmp_path: Path) -> None:
    spec = default_spec()
    div = CashDividend("AAA", date(2021, 9, 1), date(2021, 9, 15), D("0.50"), "fixture")
    spec.records["AAA"] = record("AAA", dividends=[div])
    ds = _load(tmp_path, spec)
    ca = ds.corporate_actions["AAA"]
    assert ca is not None and ca.dividends == (div,)
    rec = ds.records["AAA"]
    assert rec is not None and rec.distribution_kinds[0][1].value == "CASH_DIVIDEND"


# ===================================================================== identity and quality


def test_ticker_change_is_validated_against_the_identity_file(tmp_path: Path) -> None:
    change = date(2022, 6, 9)
    spans = [TickerSpan("OLD", date(2020, 7, 1), date(2022, 6, 8)),
             TickerSpan("AAA", change, None)]  # fmt: skip
    spec = default_spec()
    spec.identities["AAA"] = identity("AAA", spans)
    spec.source_symbols["AAA"] = {d: ("OLD" if d < change else "AAA") for d in DATES}
    rename = OtherEvent(OtherEventKind.NAME_CHANGE, change, "OLD -> AAA", "fixture")
    spec.records["AAA"] = record("AAA", others=[rename])
    assert _quality(_load(tmp_path, spec), "AAA")[0] is QualityStatus.ACCEPTED_WITH_WARNINGS
    spec.source_symbols["AAA"] = dict.fromkeys(DATES, "AAA")  # pre-change rows mislabelled
    status, issues = _quality(load_dataset(_rewrite(tmp_path, spec)), "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.IDENTITY_MISMATCH in issues
    spec.source_symbols["AAA"] = {d: ("OLD" if d < change else "AAA") for d in DATES}
    spec.records["AAA"] = record(
        "AAA", others=[replace(rename, effective_session=date(2022, 7, 1))]
    )
    status, issues = _quality(load_dataset(_rewrite(tmp_path, spec)), "AAA")
    assert QualityIssue.IDENTITY_MISMATCH in issues


def test_missing_sessions_and_late_history_are_warnings(tmp_path: Path) -> None:
    spec = default_spec()
    gap = {date(2023, 3, 1), date(2023, 3, 2)}
    spec.bars["AAA"] = [b for b in spec.bars["AAA"] if b.session_date not in gap]
    spec.bars["BBB"] = [b for b in spec.bars["BBB"] if b.session_date >= date(2022, 1, 3)]
    spec.bars["CCC"] = [b for b in spec.bars["CCC"] if b.session_date <= date(2025, 6, 30)]
    ds = _load(tmp_path, spec)
    q = next(q for q in ds.quality if q.symbol == "AAA")
    assert q.status is QualityStatus.ACCEPTED_WITH_WARNINGS
    assert q.missing_sessions == (date(2023, 3, 1), date(2023, 3, 2))
    assert q.expected_sessions == q.bar_count + 2
    assert QualityIssue.HISTORY_STARTS_LATE in _quality(ds, "BBB")[1]
    assert QualityIssue.HISTORY_ENDS_EARLY in _quality(ds, "CCC")[1]  # kept: no survivorship


def test_bar_on_a_non_session_rejects_the_series(tmp_path: Path) -> None:
    spec = default_spec()
    saturday = date(2023, 3, 4)
    extra = daily_bar("AAA", saturday, (D(50), D(51), D(49), D(50)))
    bars = list(spec.bars["AAA"])
    bars.insert(next(i for i, b in enumerate(bars) if b.session_date > saturday), extra)
    spec.bars["AAA"] = bars
    ds = _load(tmp_path, spec)
    status, issues = _quality(ds, "AAA")
    assert status is QualityStatus.REJECTED and QualityIssue.SERIES_REJECTED in issues


def test_zero_volume_sessions_are_counted(tmp_path: Path) -> None:
    spec = default_spec()
    bars = list(spec.bars["AAA"])
    b = bars[100]
    bars[100] = daily_bar("AAA", b.session_date, (b.open, b.high, b.low, b.close), volume=0)
    spec.bars["AAA"] = bars
    q = next(q for q in _load(tmp_path, spec).quality if q.symbol == "AAA")
    assert q.zero_volume_sessions == 1
    assert QualityIssue.ZERO_VOLUME_SESSIONS in {f.issue for f in q.findings}


# ===================================================================== import


def _page(rows: dict[str, list[dict[str, object]]], token: str | None = None) -> dict[str, object]:
    return {"bars": rows, "next_page_token": token}


def _row(day: str, close: str) -> dict[str, object]:
    return {"t": f"{day}T05:00:00Z", "o": Decimal("10.5"), "h": Decimal("11"), "l": 10,
            "c": Decimal(close), "v": 1000, "vw": Decimal("10.7"), "n": 12}  # fmt: skip


def test_alpaca_pages_map_through_the_adapter_validation() -> None:
    received = datetime(2026, 11, 15, tzinfo=UTC)
    pages = [
        _page({"AAA": [_row("2023-03-01", "10.75")]}, "t1"),
        _page({"AAA": [_row("2023-03-02", "10.80")], "BBB": [_row("2023-03-01", "10.6")]}),
    ]
    out = bars_from_alpaca_pages(pages, symbols=["AAA", "BBB"], start=date(2023, 3, 1),
                                 end=date(2023, 3, 2), feed="sip", adjustment="raw",
                                 received_at=received)  # fmt: skip
    assert [b.close for b in out["AAA"]] == [D("10.75"), D("10.80")]
    assert out["BBB"][0].session_date == date(2023, 3, 1)
    rows = encode_jsonl([{"x": 1}])
    assert rows == b'{"x":1}\n'
    disorder = [_page({"AAA": [_row("2023-03-02", "10.8"), _row("2023-03-01", "10.7")]})]
    with pytest.raises(DatasetFormatError):
        bars_from_alpaca_pages(disorder, symbols=["AAA"], start=date(2023, 3, 1),
                               end=date(2023, 3, 2), feed="sip", adjustment="raw",
                               received_at=received)  # fmt: skip
    floaty = [_page({"AAA": [{**_row("2023-03-01", "10.7"), "c": 10.7}]})]
    with pytest.raises(DatasetFormatError):
        bars_from_alpaca_pages(floaty, symbols=["AAA"], start=date(2023, 3, 1),
                               end=date(2023, 3, 1), feed="sip", adjustment="raw",
                               received_at=received)  # fmt: skip


def _rewrite(tmp_path: Path, spec: FixtureSpec) -> Path:
    n = len(list(tmp_path.glob("re*")))
    root = tmp_path / f"re{n}"
    write_fixture(root, spec)
    return root


# ===================================================================== evidence (16.15A)


def _split_spec(*kinds: EvidenceKind, with_adjusted: bool = True) -> FixtureSpec:
    """AAA with a real 2:1 split on 2022-06-06 whose split coverage carries ``kinds``."""
    split_day = date(2022, 6, 6)
    i = DATES.index(split_day)
    raw = list(synthetic_bars("AAA", scale=D(2)))
    after = synthetic_bars("AAA", scale=D(1))
    raw[i:] = after[i:]
    spec = default_spec()
    spec.bars["AAA"] = raw
    if with_adjusted:
        spec.split_bars["AAA"] = [
            daily_bar("AAA", b.session_date, (b.open, b.high, b.low, b.close), adjustment="split")
            for b in after
        ]
    split = SplitEvent("AAA", split_day, 2, 1, source="fixture")
    spec.records["AAA"] = record("AAA", splits=[split], split_cov=full_coverage(*kinds))
    return spec


def test_same_provider_consistency_never_attests_completeness(tmp_path: Path) -> None:
    ds = _load(tmp_path, _split_spec(EvidenceKind.SAME_PROVIDER_CONSISTENCY))
    q = next(q for q in ds.quality if q.symbol == "AAA")
    assert q.split_consistency == "PASSED"  # the internal check itself succeeded ...
    assert q.status is QualityStatus.REJECTED  # ... but it is not independent evidence
    assert QualityIssue.COVERAGE_EVIDENCE_INSUFFICIENT in {f.issue for f in q.findings}
    with pytest.raises(DatasetError):
        CoverageAcceptance(splits=frozenset({EvidenceKind.SAME_PROVIDER_CONSISTENCY}))
    with pytest.raises(DatasetError):
        CoverageAcceptance(dividends=frozenset(EvidenceKind))


def test_consistency_plus_review_is_accepted(tmp_path: Path) -> None:
    spec = _split_spec(EvidenceKind.SAME_PROVIDER_CONSISTENCY, EvidenceKind.OPERATOR_REVIEW)
    ds = _load(tmp_path, spec)
    q = next(q for q in ds.quality if q.symbol == "AAA")
    assert q.status is QualityStatus.ACCEPTED and q.split_consistency == "PASSED"


def test_consistency_evidence_must_be_rechecked_from_the_dataset(tmp_path: Path) -> None:
    spec = _split_spec(EvidenceKind.SAME_PROVIDER_CONSISTENCY, EvidenceKind.OPERATOR_REVIEW,
                       with_adjusted=False)  # fmt: skip
    status, issues = _quality(_load(tmp_path, spec), "AAA")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.EVIDENCE_UNVERIFIABLE in issues
    spec = default_spec()
    spec.records["BBB"] = record("BBB", div_cov=full_coverage(
        EvidenceKind.SAME_PROVIDER_CONSISTENCY, EvidenceKind.OPERATOR_REVIEW))  # fmt: skip
    status, issues = _quality(load_dataset(_rewrite(tmp_path, spec)), "BBB")
    assert status is QualityStatus.REJECTED
    assert QualityIssue.EVIDENCE_UNVERIFIABLE in issues


def test_evidence_needs_a_source_and_a_reference() -> None:
    with pytest.raises(DatasetFormatError):
        Evidence(EvidenceKind.OPERATOR_REVIEW, "reviewer", " ")
    with pytest.raises(DatasetFormatError):
        Coverage(date(2021, 1, 4), date(2021, 2, 1), ())


def test_manifest_records_which_evidence_supports_each_claim(tmp_path: Path) -> None:
    spec = _split_spec(EvidenceKind.SAME_PROVIDER_CONSISTENCY, EvidenceKind.OPERATOR_REVIEW)
    spec.records["CCC"] = None
    root = tmp_path / "ds"
    write_fixture(root, spec)
    manifest = json.loads((root / "manifest.json").read_text())
    claims = {(c["symbol"], c["list"]): c["claim"] for c in manifest["corporate_action_claims"]}
    assert ("CCC", "splits") not in claims  # no record => no claim, not an empty one
    kinds = [e["kind"] for e in claims[("AAA", "splits")]["evidence"]]
    assert kinds == ["SAME_PROVIDER_CONSISTENCY", "OPERATOR_REVIEW"]
    assert [e["kind"] for e in claims[("BBB", "dividends")]["evidence"]] == ["OPERATOR_REVIEW"]
    # a manifest whose claims disagree with the files is refused even with a valid digest
    _writable(root)
    claims_rows = manifest["corporate_action_claims"]
    claims_rows[0]["claim"]["evidence"][0]["kind"] = "INDEPENDENT_SOURCE"
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest["manifest_sha256"] = sha256(canonical(body).encode())
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DatasetIntegrityError):
        load_dataset(root)
