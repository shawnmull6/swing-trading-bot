"""The fixed pilot universe and the offline acquisition plan (Step 16.15, Phases 1 and 3)."""

from __future__ import annotations

import itertools
from collections import Counter
from datetime import UTC, date, datetime

import pytest

from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS, CORPORATE_ACTIONS_PATH
from swing_trading_bot.broker.alpaca_read import bar_window
from swing_trading_bot.research.acquisition import (
    ADAPTER_MAX_BAR_PAGES,
    BATCH_RETRY_RESERVE,
    HOST_ORIGINS,
    READ_ATTEMPTS,
    REQUEST_BUDGET,
    BatchKind,
    Host,
    LogicalRequest,
    Phase,
    RequestAccounting,
    bar_bounds,
    build_acquisition_plan,
    build_ca_date_semantics_plan,
    build_capability_probe_plan,
    request_accounting,
    sessions_needed,
)
from swing_trading_bot.research.universe import (
    PILOT_UNIVERSE,
    AssetClass,
    MembershipPolicy,
    ResearchUniverse,
    UniverseMember,
)

EXPECTED = {
    "Technology": {"NVDA", "AMD", "MSFT", "AAPL"},
    "Communication": {"META", "GOOGL"},
    "Consumer discretionary": {"AMZN", "TSLA"},
    "Financials": {"JPM", "GS", "XLF"},
    "Industrials": {"CAT", "BA"},
    "Healthcare": {"UNH", "ABBV"},
    "Energy": {"XOM", "CVX", "XLE"},
    "Broad market": {"SPY", "QQQ"},
}


def test_pilot_universe_is_the_fixed_twenty_symbols() -> None:
    u = PILOT_UNIVERSE
    assert len(u.symbols) == 20
    assert u.symbols == tuple(sorted(u.symbols))
    by_sector: dict[str, set[str]] = {}
    for m in u.members:
        by_sector.setdefault(m.sector, set()).add(m.symbol)
    assert by_sector == EXPECTED
    classes = Counter(m.asset_class for m in u.members)
    assert classes == {AssetClass.EQUITY: 16, AssetClass.BROAD_MARKET_ETF: 2,
                       AssetClass.SECTOR_ETF: 2}  # fmt: skip
    assert u.policy is MembershipPolicy.FIXED_PRESENT_DAY_PILOT
    assert u.survivorship_bias_free is False
    assert any("survivor" in lim.lower() for lim in u.limitations)
    assert "FB" in u.member("META").expectations[0]
    assert u.fingerprint == PILOT_UNIVERSE.fingerprint
    with pytest.raises(KeyError):
        u.member("FB")  # never silently substituted


def test_universe_rejects_duplicates_and_unsorted_members() -> None:
    a = UniverseMember("AAA", "X", AssetClass.EQUITY)
    b = UniverseMember("BBB", "X", AssetClass.EQUITY)
    for members in ((a, a), (b, a)):
        with pytest.raises(ValueError, match="member"):
            ResearchUniverse("u", MembershipPolicy.FIXED_PRESENT_DAY_PILOT, members, False, ())


def test_bar_bounds_match_the_adapter() -> None:
    spans = ((date(2020, 7, 1), date(2026, 10, 31)), (date(2024, 3, 8), date(2024, 3, 11)),
             (date(2024, 11, 1), date(2024, 11, 4)))  # fmt: skip
    for start, end in spans:
        assert bar_bounds(start, end) == bar_window(start, end)


PROBE_FINGERPRINT = "1bfd4451e9df58e7a7b6de64c072bafc8298c3e509db05af15cd592386909931"
ACQUISITION_FINGERPRINT = "6ca96c0601a404b19bd5adac44bc1e46141a9febd9c66db7b9438a7380ae5a3f"
CA_SEMANTICS_FINGERPRINT = "e586916efa62c41591bde68842b554707e19aeff4b54fc8b10f1174a7c1c4efa"


def test_fingerprints_are_deterministic_and_pinned() -> None:
    """A change to either plan must be deliberate: it changes what is authorized."""
    assert build_capability_probe_plan().fingerprint == PROBE_FINGERPRINT
    assert build_acquisition_plan().fingerprint == ACQUISITION_FINGERPRINT
    assert build_ca_date_semantics_plan().fingerprint == CA_SEMANTICS_FINGERPRINT


def test_capability_probe_definitions_are_exactly_p1_to_p6() -> None:
    plan = build_capability_probe_plan()
    got = {r.request_id: (r.host, r.path, dict(r.params), r.expected_pages,
                          r.planning_max_pages) for r in plan.requests}  # fmt: skip
    window = {"start": "2020-07-01", "end": "2026-10-31"}
    bars = {"timeframe": "1Day", "feed": "sip", "sort": "asc"}
    assert got == {
        "P1": (Host.TRADING, "/v2/calendar", {**window, "date_type": "TRADING"}, 1, 1),
        "P2": (Host.TRADING, "/v2/calendar", {**window, "date_type": "SETTLEMENT"}, 1, 1),
        "P3": (Host.DATA, "/v2/stocks/bars", {**bars, "symbols": "AAPL", "adjustment": "raw",
               "start": "2020-07-01T03:59:59Z", "end": "2020-12-31T05:00:01Z",
               "limit": "100"}, 2, 2),
        "P4": (Host.DATA, "/v2/stocks/bars", {**bars, "symbols": "AAPL", "adjustment": "split",
               "start": "2020-08-24T03:59:59Z", "end": "2020-09-04T04:00:01Z",
               "limit": "10000"}, 1, 1),
        "P5": (Host.DATA, "/v2/stocks/bars", {**bars, "symbols": "META", "adjustment": "raw",
               "start": "2022-06-06T03:59:59Z", "end": "2022-06-10T04:00:01Z",
               "limit": "10000"}, 1, 1),
        "P6": (Host.DATA, "/v1/corporate-actions", {"symbols": "AAPL,SPY",
               "types": "forward_split,cash_dividend", "start": "2020-07-01",
               "end": "2021-06-30", "limit": "5", "sort": "asc"}, 2, 2),
    }  # fmt: skip
    assert [r.request_id for r in plan.requests] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert all(r.phase is Phase.PROBE and not r.live_verified for r in plan.requests)
    # no probe reads a holdout-period (2025-2026) price
    for r in plan.requests:
        if r.path != "/v2/calendar":
            assert dict(r.params)["end"] < "2025"


def test_capability_probe_accounting_and_limits() -> None:
    plan = build_capability_probe_plan()
    assert plan.expected_http_requests == 8 == plan.max_pages
    assert plan.max_http_attempts == 10 and plan.retry_reserve == 2
    assert plan.max_attempts_per_page == 2
    assert plan.retry_on == ("HTTP_429", "HTTP_503", "TIMEOUT")
    assert plan.stop_on == ("HTTP_401", "HTTP_403")
    assert (plan.deadline_s, plan.connect_timeout_s, plan.read_timeout_s) == (120, 5, 20)
    assert plan.continues_into_acquisition is False
    assert plan.evidence_prefix == "history-probe-"
    assert dict(plan.origins) == {"trading": ALLOWED_ORIGINS["trading"],
                                  "data": ALLOWED_ORIGINS["data"]}  # fmt: skip
    assert {str(h): o for h, o in HOST_ORIGINS.items()} == dict(ALLOWED_ORIGINS)
    assert set(plan.endpoints) == {("trading", "/v2/calendar"), ("data", "/v2/stocks/bars"),
                                   ("data", CORPORATE_ACTIONS_PATH)}  # fmt: skip
    assert {(str(r.host), r.path) for r in plan.requests} == set(plan.endpoints)
    ids = {r.request_id for r in plan.requests}
    for c in plan.checks:  # every check is answered by probes in the probe plan
        assert set(c.probe.split("+")) <= ids
    assert {c.check_id for c in plan.checks} == {"CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH",
                                                 "PAGINATION", "SPLIT-ADJ",
                                                 "TICKER-IDENTITY", "CA-SHAPE"}  # fmt: skip


def test_acquisition_plan_is_separate_from_the_probe() -> None:
    plan = build_acquisition_plan()
    probe = build_capability_probe_plan()
    assert plan.fingerprint == build_acquisition_plan().fingerprint
    assert plan.capability_probe_fingerprint == probe.fingerprint
    assert plan.capability_probe_plan_id == probe.plan_id
    phases = Counter(r.phase for r in plan.requests)
    assert phases == {Phase.CORE: 42, Phase.CONDITIONAL: 9}  # no probe request in it
    assert plan.logical_requests == 51
    assert plan.expected_http_requests == 51
    assert plan.expected_within_budget and plan.expected_http_requests <= REQUEST_BUDGET
    # every batch at its own attempt budget can exceed one session; resume covers it
    assert plan.max_http_without_retries == 121
    assert plan.worst_case_http_requests == plan.batch_attempt_total == 223
    assert plan.worst_case_http_requests < 121 * READ_ATTEMPTS  # per-batch caps bind first
    assert plan.max_sessions_without_stop == 2
    assert (plan.session_max_attempts, plan.session_deadline_s) == (REQUEST_BUDGET, 300)
    assert len(plan.batches) == plan.logical_requests
    assert any("budget" in c for c in plan.stop_conditions)
    assert any("never retried" in c for c in plan.stop_conditions)
    assert any("capability probe" in c for c in plan.stop_conditions)
    ids = {r.request_id for r in plan.requests}
    assert len(ids) == len(plan.requests)
    assert not ids & {r.request_id for r in probe.requests}
    checks = {c.check_id: c for c in plan.checks}
    for r in plan.requests:
        assert set(r.requires) <= set(checks)
        assert 1 <= r.expected_pages <= r.planning_max_pages
        if r.path == "/v2/stocks/bars":
            assert r.planning_max_pages <= ADAPTER_MAX_BAR_PAGES
    core_bars = [r for r in plan.requests
                 if r.phase is Phase.CORE and r.path == "/v2/stocks/bars"]  # fmt: skip
    assert sorted(dict(r.params)["symbols"] for r in core_bars) == sorted(
        [s for s in PILOT_UNIVERSE.symbols for _ in range(2)])  # fmt: skip
    for r in core_bars:
        p = dict(r.params)
        assert p["feed"] == "sip" and p["timeframe"] == "1Day"
        assert p["adjustment"] in {"raw", "split"}
        assert p["end"] == "2026-10-31T04:00:01Z"
        assert r.host is Host.DATA
        assert {"SIP-DEPTH", "PAGINATION"} <= set(r.requires)
    fb = [r for r in plan.requests if r.request_id.startswith("X-BARS-FB")]
    assert len(fb) == 2 and all("TICKER-IDENTITY" in r.requires for r in fb)
    actions = [r for r in plan.requests if r.path == "/v1/corporate-actions"]
    assert len(actions) == 7 and not any(r.live_verified for r in actions)
    assert all(r.requires == ("CA-SHAPE",) for r in actions)
    assert all("PROVIDER_REPORTED" in r.purpose for r in actions)
    newest = max(datetime.fromisoformat(dict(r.params)["end"].replace("Z", "+00:00"))
                 for r in plan.requests if r.path == "/v2/stocks/bars")  # fmt: skip
    assert newest < datetime(2026, 11, 16, tzinfo=UTC)
    assert any("not independent" in lim for lim in plan.limitations)


def test_corporate_action_batches_are_padded_contiguous_slices() -> None:
    plan = build_acquisition_plan()
    ca = [b for b in plan.batches if b.kind is BatchKind.CORPORATE_ACTIONS]
    assert ca[0].coverage_start == date(2020, 7, 1) and ca[-1].coverage_end == date(2026, 10, 31)
    for a, b in itertools.pairwise(ca):  # contiguous ex-date slices, overlapping requests
        assert (b.coverage_start - a.coverage_end).days == 1
        assert b.request_start < a.request_end
    for b in ca:
        assert (b.coverage_start - b.request_start).days == plan.ca_pad_before_days == 31
        assert (b.request_end - b.coverage_end).days == plan.ca_pad_after_days == 92
    assert "UNVERIFIED" in plan.ca_date_filter_observation
    assert [g.check_id for g in plan.gates] == ["CA-DATE-SEMANTICS"]


def test_ca_date_semantics_plan_is_proposed_not_part_of_the_probe() -> None:
    sem = build_ca_date_semantics_plan()
    plan = build_acquisition_plan()
    assert plan.ca_date_semantics_fingerprint == sem.fingerprint
    assert [r.request_id for r in sem.requests] == ["Q1", "Q2", "Q3"]
    days = {r.request_id: (dict(r.params)["start"], dict(r.params)["end"]) for r in sem.requests}
    assert days == {
        "Q1": ("2020-08-28", "2020-08-28"),
        "Q2": ("2020-08-31", "2020-08-31"),
        "Q3": ("2020-07-31", "2020-07-31"),
    }
    assert all(r.path == "/v1/corporate-actions" and r.host is Host.DATA for r in sem.requests)
    assert (sem.max_http_attempts, sem.deadline_s, sem.continues_into_acquisition) == (5, 60, False)
    assert set(sem.endpoints) == {("data", CORPORATE_ACTIONS_PATH)}
    probe = build_capability_probe_plan()
    assert "CA-DATE-SEMANTICS" not in {c.check_id for c in probe.checks}


def test_plan_rejects_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="warmup_start"):
        build_acquisition_plan(warmup_start=date(2021, 2, 1), research_start=date(2021, 1, 1))


# ===================================================================== request accounting
#: The capability-check outcomes recorded by the 16.16D probe (every check passed).
_CHECKS_16_16D = ("CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH", "PAGINATION", "SPLIT-ADJ",
                  "TICKER-IDENTITY", "CA-SHAPE")  # fmt: skip
GATES_16_16D = dict.fromkeys(_CHECKS_16_16D, True)


def _rows(acc: RequestAccounting) -> dict[str, tuple[int, ...]]:
    return {r.kind.value: (r.batches, r.expected_pages, r.max_pages, r.retry_allowance,
                           r.max_attempts, r.total_worst_case) for r in acc.rows}  # fmt: skip


def test_request_accounting_for_every_batch_reconciles_with_the_plan() -> None:
    plan = build_acquisition_plan()
    acc = request_accounting(plan)
    assert _rows(acc) == {
        "CALENDAR": (2, 1, 1, 2, 3, 6),
        "BARS": (42, 1, 2, 2, 4, 168),
        "CORPORATE_ACTIONS": (7, 1, 5, 2, 7, 49),
    }
    assert (acc.batches, acc.expected_requests, acc.page_cap_requests,
            acc.worst_case_attempts) == (51, 51, 121, 223)  # fmt: skip
    assert 2 * 3 + 42 * 4 + 7 * 7 == 223 and 2 * 1 + 42 * 2 + 7 * 5 == 121
    # the planner's stated maxima are the same numbers the runner enforces
    assert acc.expected_requests == plan.expected_http_requests
    assert acc.page_cap_requests == plan.max_http_without_retries
    assert acc.worst_case_attempts == plan.worst_case_http_requests == plan.batch_attempt_total
    assert acc.worst_case_attempts == sum(b.max_attempts for b in plan.batches)
    assert acc.sessions_worst_case == plan.max_sessions_without_stop == 2
    assert acc.sessions_expected == 1
    assert (acc.session_max_attempts, acc.session_deadline_s) == (REQUEST_BUDGET, 300)


def test_request_accounting_under_the_16_16d_gate_outcomes() -> None:
    acc = request_accounting(build_acquisition_plan(), GATES_16_16D)
    assert _rows(acc)["BARS"] == (40, 1, 2, 2, 4, 160)  # the FB fallback is skipped
    assert (acc.batches, acc.expected_requests, acc.page_cap_requests,
            acc.worst_case_attempts) == (49, 49, 117, 215)  # fmt: skip
    assert (acc.sessions_expected, acc.sessions_worst_case) == (1, 2)
    assert sorted(r for _, r in acc.skipped) == ["only if TICKER-IDENTITY failed"] * 2
    failed = request_accounting(build_acquisition_plan(),
                                {**GATES_16_16D, "TICKER-IDENTITY": False})  # fmt: skip
    assert failed.batches == 51 and failed.worst_case_attempts == 223


def test_every_batch_budget_follows_the_one_rule_and_never_raises_a_limit() -> None:
    plan = build_acquisition_plan()
    reqs = {r.request_id: r for r in plan.requests}
    for b in plan.batches:
        req = reqs[b.request_id]
        assert b.max_attempts == req.attempt_budget
        assert b.max_attempts == min(b.max_pages + BATCH_RETRY_RESERVE,
                                     b.max_pages * READ_ATTEMPTS)  # fmt: skip
        assert b.max_pages == req.planning_max_pages <= ADAPTER_MAX_BAR_PAGES
        assert b.max_attempts <= plan.session_max_attempts
    assert (plan.session_max_attempts, plan.session_deadline_s) == (REQUEST_BUDGET, 300)
    assert (plan.batch_retry_reserve, plan.max_attempts_per_page) == (2, READ_ATTEMPTS)
    assert not hasattr(LogicalRequest, "worst_case_http")  # the stale pages x 3 bound is gone


def test_sessions_needed_mirrors_the_runner_reservation_rule() -> None:
    # 120-attempt sessions; a batch starts only if its whole budget still fits
    assert sessions_needed([]) == 0
    assert sessions_needed([(7, 7)] * 17) == 1  # 119 used
    assert sessions_needed([(7, 7)] * 18) == 2  # 1 left < 7: the 18th waits
    assert sessions_needed([(7, 1)] * 100) == 1  # reserving is not consuming
    assert sessions_needed([(4, 4)] * 30 + [(7, 7)]) == 2  # 120 used: no room for 7
    with pytest.raises(ValueError, match="cannot fit"):
        sessions_needed([(121, 1)])
