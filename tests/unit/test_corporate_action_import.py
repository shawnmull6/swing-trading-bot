"""Corporate-action date semantics, padded windows, de-duplication and completeness gates
(Step 16.16E, Task 1/3). Every response is SYNTHETIC; the shapes follow the 16.16D probe."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from swing_trading_bot.research.acquisition import (
    CA_PAD_AFTER_DAYS,
    CA_PAD_BEFORE_DAYS,
    CA_SEMANTICS_REFERENCES,
    CorporateActionWindow,
    ca_windows,
)
from swing_trading_bot.research.corporate_actions import (
    ACTION_TYPES,
    ANY_LISTED_DATE,
    OBSERVED_16_16D,
    CorporateActionImport,
    CorporateActionParseError,
    DateRole,
    FilterSemantics,
    ImportIssue,
    ProviderEvent,
    SemanticsStatus,
    WindowSelection,
    build_records,
    discriminated_types,
    discriminating_events,
    evaluate_semantics_probe,
    expected_pattern,
    hypothesis_table,
    parse_page,
    select_window,
)
from swing_trading_bot.research.dataset import Evidence, EvidenceKind, OtherEventKind

UNIVERSE = ("AAPL", "META", "SPY")
#: Verified for every action type (a hypothetical fully-discriminating probe).
VERIFIED = FilterSemantics(
    SemanticsStatus.LIVE_VERIFIED, (DateRole.PROCESS_DATE,), "test", tuple(sorted(ACTION_TYPES))
)
TARGET = (date(2020, 7, 1), date(2021, 12, 31))


def dividend(pid: str, sym: str, ex: date, *, pay_lag: int = 6, rate: str = "0.205",
             special: bool = False) -> dict[str, Any]:  # fmt: skip
    pay = ex + timedelta(days=pay_lag)
    return {"id": pid, "symbol": sym, "cusip": "000000000", "ex_date": str(ex),
            "record_date": str(ex + timedelta(days=1)), "payable_date": str(pay),
            "process_date": str(pay), "rate": Decimal(rate), "special": special,
            "foreign": False}  # fmt: skip


def split(pid: str, sym: str, ex: date, new: int = 4, old: int = 1) -> dict[str, Any]:
    return {"id": pid, "symbol": sym, "cusip": "000000000", "ex_date": str(ex),
            "record_date": str(ex - timedelta(days=7)),
            "payable_date": str(ex - timedelta(days=3)), "process_date": str(ex),
            "due_bill_redemption_date": str(ex + timedelta(days=1)), "new_rate": new,
            "old_rate": old}  # fmt: skip


def body(lists: dict[str, list[dict[str, Any]]], token: str | None = None) -> bytes:
    def enc(o: object) -> str:
        if isinstance(o, Decimal):
            return f"@@{o}@@"
        raise TypeError(o)

    text = json.dumps({"corporate_actions": lists, "next_page_token": token}, default=enc)
    # Decimal values must stay JSON numbers, as Alpaca sends them
    return text.replace('"@@', "").replace('@@"', "").encode()


def provider(events: dict[str, list[dict[str, Any]]], window: CorporateActionWindow,
             role: str = "process_date") -> list[ProviderEvent]:  # fmt: skip
    """A provider that filters on ``role`` within the padded request window."""
    lo, hi = str(window.request_start), str(window.request_end)
    kept = {k: [e for e in v if lo <= e[role] <= hi] for k, v in events.items()}
    return list(parse_page(body(kept), UNIVERSE).events)


def windows() -> tuple[CorporateActionWindow, ...]:
    return ca_windows(*TARGET)


def run(events: dict[str, list[dict[str, Any]]], *, semantics: FilterSemantics = VERIFIED,
        role: str = "process_date", **kw: Any) -> CorporateActionImport:  # fmt: skip
    sels = [select_window(f"B{w.coverage_start.year}", w, provider(events, w, role), semantics)
            for w in windows()]  # fmt: skip
    return build_records(sels, symbols=UNIVERSE, target=TARGET, semantics=semantics, **kw)


def issues(imp: CorporateActionImport) -> set[ImportIssue]:
    return {f.issue for f in imp.findings}


# ============================================================ parsing
def test_parses_the_probe_shape_exactly() -> None:
    page = parse_page(body({"cash_dividends": [dividend("d1", "SPY", date(2020, 6, 19),
                                                        pay_lag=42, rate="1.3662419")],
                            "forward_splits": [split("s1", "AAPL", date(2020, 8, 31))]},
                           "tok"), UNIVERSE)  # fmt: skip
    assert page.next_page_token == "tok"
    by_id = {e.provider_id: e for e in page.events}
    assert by_id["d1"].effective_date == date(2020, 6, 19)  # the ex-date, not the pay date
    assert by_id["d1"].date_of(DateRole.PROCESS_DATE) == date(2020, 7, 31)
    assert by_id["d1"].raw["rate"] == Decimal("1.3662419")  # exact, never a float
    assert by_id["s1"].effective_date == date(2020, 8, 31)
    assert by_id["s1"].date_of(DateRole.PAYABLE_DATE) == date(2020, 8, 28)
    assert all(e.shape_verified for e in page.events)


@pytest.mark.parametrize(
    ("blob", "code"),
    [
        (b'{"corporate_actions": {}}', "UNEXPECTED_SHAPE"),
        (b'{"corporate_actions": {}, "next_page_token": null, "x": 1}', "UNEXPECTED_SHAPE"),
        (b'{"corporate_actions": {"mystery": []}, "next_page_token": null}', "UNKNOWN_ACTION_TYPE"),
        (b'{"corporate_actions": {}, "next_page_token": ""}', "INVALID_PAGE_TOKEN"),
        (
            b'{"corporate_actions": {"cash_dividends": [{"symbol": "SPY"}]},'
            b' "next_page_token": null}',
            "INVALID_EVENT",
        ),
        (
            b'{"corporate_actions": {"cash_dividends": [{"id": "x", "symbol": "SPY",'
            b' "ex_date": "06/19/2020"}]}, "next_page_token": null}',
            "INVALID_DATE",
        ),
        (
            b'{"corporate_actions": {"spin_offs": [{"id": "x", "process_date": "2021-01-04"}]},'
            b' "next_page_token": null}',
            "UNRECOGNIZED_EVENT_SHAPE",
        ),
        (b'{"corporate_actions": {}, "next_page_token": NaN}', "INVALID_JSON"),
    ],
)
def test_unrecognized_responses_fail_closed(blob: bytes, code: str) -> None:
    with pytest.raises(CorporateActionParseError) as err:
        parse_page(blob, UNIVERSE)
    assert err.value.code == code


# ============================================================ date semantics
def test_windows_are_contiguous_slices_with_padded_requests() -> None:
    ws = windows()
    assert [(w.coverage_start, w.coverage_end) for w in ws] == [
        (date(2020, 7, 1), date(2020, 12, 31)),
        (date(2021, 1, 1), date(2021, 12, 31)),
    ]
    assert ws[0].request_start == date(2020, 7, 1) - timedelta(days=CA_PAD_BEFORE_DAYS)
    assert ws[1].request_end == date(2021, 12, 31) + timedelta(days=CA_PAD_AFTER_DAYS)
    assert ws[0].request_end > ws[1].request_start  # the requests overlap


def test_events_before_within_and_after_the_window_are_selected_by_ex_date() -> None:
    """The 16.16D case: an ex-date before the coverage start is returned (its process
    date is inside the request) and must NOT enter the coverage; one whose ex-date is
    inside but whose process date is after the coverage end must."""
    w = CorporateActionWindow(date(2020, 7, 1), date(2021, 6, 30))
    events = [
        *parse_page(body({"cash_dividends": [
            dividend("before", "SPY", date(2020, 6, 19), pay_lag=42),   # ex before coverage
            dividend("inside", "SPY", date(2020, 9, 18), pay_lag=42),
            dividend("late-pay", "SPY", date(2021, 6, 18), pay_lag=42),  # pays 07-30
            dividend("after", "SPY", date(2021, 7, 20), pay_lag=10),     # ex after coverage
        ]}), UNIVERSE).events]  # fmt: skip
    sel = select_window("B", w, events, VERIFIED)
    assert {e.provider_id for e in sel.in_coverage} == {"inside", "late-pay"}
    assert {e.provider_id for e in sel.pad_zone} == {"before", "after"}


def test_unpadded_requests_would_lose_the_june_2021_dividend() -> None:
    """Why padding exists: filtering on the process date, an unpadded 2021-H1 request
    misses SPY's 2021-06-18 ex-date dividend (paid 07-30)."""
    events = {"cash_dividends": [dividend("jun", "SPY", date(2021, 6, 18), pay_lag=42)]}
    unpadded = CorporateActionWindow(date(2021, 1, 1), date(2021, 6, 30), 0, 0)
    padded = CorporateActionWindow(date(2021, 1, 1), date(2021, 6, 30))
    assert provider(events, unpadded) == []
    assert [e.provider_id for e in provider(events, padded)] == ["jun"]


def test_semantics_probe_evaluation() -> None:
    process = evaluate_semantics_probe({"Q1": False, "Q2": True, "Q3": True})
    payable = evaluate_semantics_probe({"Q1": True, "Q2": False, "Q3": True})
    assert process.verified_role is DateRole.PROCESS_DATE
    assert payable.verified_role is DateRole.PAYABLE_DATE
    # the ex-date was already excluded by the 16.16D evidence: inconsistent -> inconclusive
    ex = evaluate_semantics_probe({"Q1": False, "Q2": True, "Q3": False})
    assert ex.status is SemanticsStatus.INCONCLUSIVE and ex.verified_role is None
    for answers in ({"Q1": False, "Q2": False, "Q3": False}, {"Q1": True, "Q2": True,
                    "Q3": True}, {"Q1": True}, {"Q1": True, "Q2": False, "Q3": False}):  # fmt: skip
        assert evaluate_semantics_probe(answers).status is SemanticsStatus.INCONCLUSIVE
    assert OBSERVED_16_16D.verified_role is None


# ============================================================ date-hypothesis discrimination
def test_references_carry_only_the_dates_recorded_by_16_16d() -> None:
    """Copied from history-probe-20261117T025839Z raw/006 and raw/007; nothing inferred."""
    q1, q2, q3 = (CA_SEMANTICS_REFERENCES[q] for q in ("Q1", "Q2", "Q3"))
    for q in (q1, q2):
        assert (q.symbol, q.list_name) == ("AAPL", "forward_splits")
        assert q.dates() == {"ex_date": date(2020, 8, 31), "record_date": date(2020, 8, 24),
                             "payable_date": date(2020, 8, 28),
                             "process_date": date(2020, 8, 31)}  # fmt: skip
    assert (q1.requested_day, q2.requested_day) == (date(2020, 8, 28), date(2020, 8, 31))
    assert (q3.symbol, q3.list_name, q3.requested_day) == ("SPY", "cash_dividends",
                                                            date(2020, 7, 31))  # fmt: skip
    assert q3.dates() == {
        "ex_date": date(2020, 6, 19),
        "record_date": date(2020, 6, 22),
        "payable_date": date(2020, 7, 31),
        "process_date": date(2020, 7, 31),
    }
    assert all("history-probe-20261117T025839Z" in r.evidence
               for r in CA_SEMANTICS_REFERENCES.values())  # fmt: skip


def test_expected_responses_per_hypothesis_are_derived_and_distinct() -> None:
    table = hypothesis_table()
    assert table == {
        "ex_date": (False, True, False),
        "record_date": (False, False, False),
        "payable_date": (True, False, True),
        "process_date": (False, True, True),
        ANY_LISTED_DATE: (True, True, True),
    }
    assert expected_pattern(DateRole.PAYABLE_DATE) != expected_pattern(DateRole.PROCESS_DATE)
    assert len(set(table.values())) == len(table)  # every hypothesis answers differently


@pytest.mark.parametrize("role", [DateRole.PAYABLE_DATE, DateRole.PROCESS_DATE])
def test_a_simulated_provider_is_identified_from_its_own_answers(role: DateRole) -> None:
    """Answer each reference request as a provider filtering on ``role`` would."""
    answers = {q: r.dates()[role.value] == r.requested_day
               for q, r in CA_SEMANTICS_REFERENCES.items()}  # fmt: skip
    result = evaluate_semantics_probe(answers)
    assert result.status is SemanticsStatus.LIVE_VERIFIED and result.verified_role is role
    assert result.scope == ("forward_splits",)


def test_the_probe_cannot_discriminate_for_cash_dividends() -> None:
    """Q3's dividend has payable == process: the result is scoped to forward splits."""
    assert discriminated_types(DateRole.PAYABLE_DATE, [DateRole.PROCESS_DATE]) == (
        "forward_splits",
    )
    assert discriminated_types(DateRole.PROCESS_DATE, [DateRole.PAYABLE_DATE]) == (
        "forward_splits",
    )
    verified = evaluate_semantics_probe({"Q1": False, "Q2": True, "Q3": True})
    assert verified.role_for("forward_splits") is DateRole.PROCESS_DATE
    for other in ("cash_dividends", "reverse_splits", "spin_offs"):
        assert verified.role_for(other) is None
        # outside the scope every prior candidate is still checked
        assert verified.roles_for(other) == (DateRole.PAYABLE_DATE, DateRole.PROCESS_DATE)
    assert OBSERVED_16_16D.roles_for("forward_splits") == (DateRole.PAYABLE_DATE,
                                                            DateRole.PROCESS_DATE)  # fmt: skip


def test_a_split_scoped_verification_still_withholds_every_provider_claim() -> None:
    """Splits include unprobed reverse splits; dividends were never discriminated; so a
    successful probe alone unblocks no PROVIDER_REPORTED claim."""
    verified = evaluate_semantics_probe({"Q1": False, "Q2": True, "Q3": True})
    imp = run({"cash_dividends": [dividend("d", "SPY", date(2021, 3, 19), pay_lag=42)],
               "forward_splits": [split("s", "AAPL", date(2020, 8, 31))]},
              semantics=verified)  # fmt: skip
    blocked = {f.list_name for f in imp.findings
               if f.issue is ImportIssue.DATE_SEMANTICS_UNVERIFIED}  # fmt: skip
    assert blocked == {"splits", "dividends", "other_events"}
    assert all("PROVIDER_REPORTED" not in kinds for c in imp.claims.values()
               for kinds in c.values())  # fmt: skip


def test_padding_checks_every_unverified_candidate_but_only_the_verified_role() -> None:
    """A dividend whose payable date is far beyond the padding: flagged while payable is
    a candidate for dividends; a split whose payable date lags the same way is not
    flagged once process is verified for splits."""
    # seen only by the 2021 batch, whose window holds both dates (no role is excluded)
    late = {**dividend("d", "SPY", date(2021, 8, 2)), "payable_date": "2021-12-30"}
    sp = {**split("s", "AAPL", date(2021, 8, 2)), "payable_date": "2021-12-30"}
    verified = evaluate_semantics_probe({"Q1": False, "Q2": True, "Q3": True})
    imp = run({"cash_dividends": [late], "forward_splits": [sp]}, semantics=verified)
    margin = [(f.action_type, f.role) for f in imp.findings
              if f.issue is ImportIssue.PAD_MARGIN_EXCEEDED]  # fmt: skip
    assert ("cash_dividends", "payable_date") in margin
    assert ("forward_splits", "payable_date") not in margin


def test_discovery_helper_finds_only_events_whose_two_dates_differ() -> None:
    events = parse_page(body({"cash_dividends": [dividend("d", "SPY", date(2020, 6, 19),
                                                          pay_lag=42)],
                              "forward_splits": [split("s", "AAPL", date(2020, 8, 31))]}),
                        UNIVERSE).events  # fmt: skip
    assert [e.provider_id for e in discriminating_events(events)] == ["s"]


def test_observations_narrow_but_never_verify_the_semantics() -> None:
    imp = run({"cash_dividends": [dividend("d", "SPY", date(2020, 6, 19), pay_lag=42)]},
              semantics=OBSERVED_16_16D)  # fmt: skip
    assert imp.semantics.status is SemanticsStatus.CONSISTENT_WITH_OBSERVATIONS
    assert imp.semantics.verified_role is None
    assert ImportIssue.DATE_SEMANTICS_UNVERIFIED in issues(imp)


# ============================================================ duplicates and cross-checks
def test_duplicates_across_padded_windows_are_merged_once() -> None:
    """A December dividend paid in January is returned by both yearly requests."""
    imp = run({"cash_dividends": [dividend("dec", "AAPL", date(2020, 12, 10), pay_lag=30)]})
    assert [d.ex_session for d in imp.records["AAPL"].dividends] == [date(2020, 12, 10)]
    assert not issues(imp) & {ImportIssue.DUPLICATE_EVENT_CONFLICT,
                              ImportIssue.AMBIGUOUS_DUPLICATE}  # fmt: skip
    assert imp.claims["AAPL"]["dividends"] == ("PROVIDER_REPORTED",)


def _selections(first: Sequence[dict[str, Any]], second: Sequence[dict[str, Any]]
                ) -> list[WindowSelection]:  # fmt: skip
    w1, w2 = windows()
    return [select_window("B2020", w1, list(parse_page(body({"cash_dividends": list(first)}),
                                                       UNIVERSE).events), VERIFIED),
            select_window("B2021", w2, list(parse_page(body({"cash_dividends": list(second)}),
                                                       UNIVERSE).events), VERIFIED)]  # fmt: skip


def test_a_conflicting_duplicate_fails_closed_for_that_symbol() -> None:
    a = dividend("dec", "AAPL", date(2020, 12, 10), pay_lag=30)
    b = {**a, "rate": Decimal("0.99")}
    imp = build_records(_selections([a], [b]), symbols=UNIVERSE, target=TARGET,
                        semantics=VERIFIED)  # fmt: skip
    assert ImportIssue.DUPLICATE_EVENT_CONFLICT in issues(imp)
    assert imp.records["AAPL"].dividends == ()  # neither version is guessed
    assert imp.records["AAPL"].dividend_coverage is None
    assert imp.records["SPY"].dividend_coverage is not None  # other symbols unaffected


def test_same_event_under_two_ids_is_ambiguous() -> None:
    a = dividend("x1", "AAPL", date(2021, 2, 5))
    b = {**dividend("x2", "AAPL", date(2021, 2, 5)), "rate": Decimal("0.21")}
    imp = build_records(_selections([], [a, b]), symbols=UNIVERSE, target=TARGET,
                        semantics=VERIFIED)  # fmt: skip
    assert ImportIssue.AMBIGUOUS_DUPLICATE in issues(imp)
    assert imp.records["AAPL"].dividend_coverage is None


def test_a_pad_zone_event_missing_from_its_owner_blocks_every_claim() -> None:
    """2020's padded request sees a January-2021 dividend that 2021's own request did not
    return: the filter semantics are not what we assumed; no provider claim is made."""
    jan = dividend("jan", "AAPL", date(2021, 1, 8))
    imp = build_records(_selections([jan], []), symbols=UNIVERSE, target=TARGET,
                        semantics=VERIFIED)  # fmt: skip
    assert ImportIssue.PAD_CROSS_CHECK_MISSING in issues(imp)
    assert imp.records["SPY"].dividend_coverage is None
    assert imp.records["AAPL"].dividends == ()  # pad-zone events never enter a record


# ============================================================ completeness gates
def test_claims_need_verified_semantics_and_margin() -> None:
    events = {
        "cash_dividends": [dividend("d", "SPY", date(2021, 3, 19), pay_lag=42)],
        "forward_splits": [split("s", "AAPL", date(2020, 8, 31))],
    }
    ok = run(events)
    assert ok.claims["SPY"]["dividends"] == ("PROVIDER_REPORTED",)
    assert ok.records["AAPL"].splits[0].ratio == 4
    assert not ok.findings
    late = run({"cash_dividends": [dividend("d", "SPY", date(2021, 3, 19), pay_lag=85)]})
    assert ImportIssue.PAD_MARGIN_EXCEEDED in issues(late)
    assert all(not late.claims[s]["dividends"] for s in UNIVERSE)  # padding shown unsafe
    unverified = run(events, semantics=OBSERVED_16_16D)
    assert all(not unverified.claims[s][n] for s in UNIVERSE for n in unverified.claims[s])


def test_a_missing_batch_is_a_coverage_gap() -> None:
    w1, _ = windows()
    sel = select_window("B2020", w1, [], VERIFIED)
    imp = build_records([sel], symbols=UNIVERSE, target=TARGET, semantics=VERIFIED)
    assert ImportIssue.COVERAGE_GAP in issues(imp)
    assert all(r.split_coverage is None for r in imp.records.values())


def test_attestations_attach_but_never_same_provider_evidence() -> None:
    review = Evidence(EvidenceKind.OPERATOR_REVIEW, "reviewer", "review-001")
    imp = run({"cash_dividends": []}, semantics=OBSERVED_16_16D,
              attestations={("SPY", "dividends"): [review]})  # fmt: skip
    cov = imp.records["SPY"].dividend_coverage
    assert cov is not None and cov.kinds == {EvidenceKind.OPERATOR_REVIEW}
    assert (cov.start, cov.end) == TARGET
    for kind in (EvidenceKind.SAME_PROVIDER_CONSISTENCY, EvidenceKind.PROVIDER_REPORTED):
        with pytest.raises(ValueError, match="attestations"):
            run({}, attestations={("SPY", "dividends"): [Evidence(kind, "x", "y")]})


def test_unsupported_and_unverified_types_are_kept_visible() -> None:
    spin = {"id": "so1", "source_symbol": "META", "new_symbol": "NEWCO",
            "ex_date": "2021-04-01", "process_date": "2021-04-01"}  # fmt: skip
    imp = run({"spin_offs": [spin]})
    assert [e.kind for e in imp.records["META"].other_events] == [OtherEventKind.SPIN_OFF]
    assert ImportIssue.UNVERIFIED_EVENT_SHAPE in issues(imp)
    nobody = {"id": "x", "acquirer_symbol": "ZZZ", "ex_date": "2021-04-01",
              "process_date": "2021-04-01"}  # fmt: skip
    imp = run({"cash_mergers": [nobody]})
    assert ImportIssue.UNATTRIBUTED_EVENT in issues(imp)
    assert all(not imp.claims[s]["other_events"] for s in UNIVERSE)


def test_invalid_events_block_only_their_symbol() -> None:
    bad = {**split("s", "AAPL", date(2021, 6, 1)), "new_rate": Decimal("1.5")}
    imp = run({"forward_splits": [bad]})
    assert ImportIssue.EVENT_INVALID in issues(imp)
    assert imp.records["AAPL"].split_coverage is None
    assert imp.records["SPY"].split_coverage is not None
