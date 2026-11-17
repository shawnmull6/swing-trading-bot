"""The fixed pilot universe and the offline acquisition plan (Step 16.15, Phases 1 and 3)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime

import pytest

from swing_trading_bot.broker.alpaca_read import bar_window
from swing_trading_bot.research.acquisition import (
    ADAPTER_MAX_BAR_PAGES,
    READ_ATTEMPTS,
    REQUEST_BUDGET,
    Host,
    Phase,
    bar_bounds,
    build_acquisition_plan,
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


def test_acquisition_plan_separates_logical_http_and_probe_requests() -> None:
    plan = build_acquisition_plan()
    assert plan.fingerprint == build_acquisition_plan().fingerprint
    phases = Counter(r.phase for r in plan.requests)
    assert phases == {Phase.PROBE: 7, Phase.CORE: 42, Phase.CONDITIONAL: 9}
    assert plan.logical_requests == 58
    assert plan.expected_http_requests == 60  # P-PAGES expects 3 pages
    assert plan.expected_within_budget and plan.expected_http_requests <= REQUEST_BUDGET
    # caps and retries can exceed the budget; the budget then stops the session
    assert plan.max_http_without_retries == 131
    assert plan.worst_case_http_requests == 131 * READ_ATTEMPTS
    assert any("budget" in c for c in plan.stop_conditions)
    assert any("never retried" in c for c in plan.stop_conditions)
    ids = {r.request_id for r in plan.requests}
    assert len(ids) == len(plan.requests)
    checks = {c.check_id: c for c in plan.checks}
    for r in plan.requests:
        assert set(r.requires) <= set(checks)
        assert 1 <= r.expected_pages <= r.planning_max_pages
        if r.path == "/v2/stocks/bars":
            assert r.planning_max_pages <= ADAPTER_MAX_BAR_PAGES
    for c in plan.checks:  # every check is answered by probes in the plan
        assert set(c.probe.split("+")) <= ids
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
    actions = [r for r in plan.requests if r.path == "/v1/corporate-actions"]
    assert len(actions) == 8 and not any(r.live_verified for r in actions)
    assert all(r.requires == ("CA-SHAPE",) for r in actions if r.phase is Phase.CONDITIONAL)
    assert all("PROVIDER_REPORTED" in r.purpose for r in actions if r.phase is Phase.CONDITIONAL)
    newest = max(datetime.fromisoformat(dict(r.params)["end"].replace("Z", "+00:00"))
                 for r in plan.requests if r.path == "/v2/stocks/bars")  # fmt: skip
    assert newest < datetime(2026, 11, 16, tzinfo=UTC)
    assert any("not independent" in lim for lim in plan.limitations)


def test_plan_rejects_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="warmup_start"):
        build_acquisition_plan(warmup_start=date(2021, 2, 1), research_start=date(2021, 1, 1))
