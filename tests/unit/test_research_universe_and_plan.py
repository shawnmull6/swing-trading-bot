"""The fixed pilot universe and the offline acquisition plan (Step 16.15, Phases 1 and 3)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime

import pytest

from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS, CORPORATE_ACTIONS_PATH
from swing_trading_bot.broker.alpaca_read import bar_window
from swing_trading_bot.research.acquisition import (
    ADAPTER_MAX_BAR_PAGES,
    HOST_ORIGINS,
    READ_ATTEMPTS,
    REQUEST_BUDGET,
    Host,
    Phase,
    bar_bounds,
    build_acquisition_plan,
    build_capability_probe_plan,
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
ACQUISITION_FINGERPRINT = "c4d5d57649b501843fd96879c01286cd3947e4994c61306a36321fb6a825b7e9"


def test_fingerprints_are_deterministic_and_pinned() -> None:
    """A change to either plan must be deliberate: it changes what is authorized."""
    assert build_capability_probe_plan().fingerprint == PROBE_FINGERPRINT
    assert build_acquisition_plan().fingerprint == ACQUISITION_FINGERPRINT


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
    # caps and retries can exceed the budget; the budget then stops the session
    assert plan.max_http_without_retries == 121
    assert plan.worst_case_http_requests == 121 * READ_ATTEMPTS
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


def test_plan_rejects_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="warmup_start"):
        build_acquisition_plan(warmup_start=date(2021, 2, 1), research_start=date(2021, 1, 1))
