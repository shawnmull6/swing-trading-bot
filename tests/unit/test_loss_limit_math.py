"""Step 7: pure loss-limit and gap-stress arithmetic, with property-based tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.loss_limits import (
    GapObservation,
    LimitMeasure,
    gap_stress,
    is_breached,
    loss_fraction,
)

D = Decimal


def test_thresholds_on_initial_portfolio() -> None:
    daily = LimitMeasure(D(5000), D(5000), D("0.02"))
    weekly = LimitMeasure(D(5000), D(5000), D("0.05"))
    assert (daily.budget, weekly.budget) == (D(100), D(250))
    assert not is_breached(D(5000), D("4900.01"), D("0.02"))
    assert is_breached(D(5000), D("4900.00"), D("0.02"))  # inclusive at exactly 2%
    assert LimitMeasure(D(5000), D(4800), D("0.02")).remaining == 0
    assert LimitMeasure(D(5000), D(5100), D("0.02")).loss_pct == D("-0.020000")


def test_invalid_baseline_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        is_breached(D(0), D(1), D("0.02"))
    with pytest.raises(ValueError, match="positive"):
        loss_fraction(D(-1), D(1))
    with pytest.raises(ValueError, match="positive"):
        gap_stress({}, D(0), denominator=D(0), scenario_pct=D("0.15"),
                   position_limit_pct=D("0.03"), portfolio_limit_pct=D("0.10"))  # fmt: skip


def test_gap_stress_boundaries() -> None:
    r = gap_stress(
        {"AAPL": D("1000"), "MSFT": D("1000.01")},
        D("0"),
        denominator=D(5000),
        scenario_pct=D("0.15"),
        position_limit_pct=D("0.03"),
        portfolio_limit_pct=D("0.10"),
    )
    assert [p.stressed_loss for p in r.positions] == [D("150.00"), D("150.0015")]
    assert r.positions_exceeded == ("MSFT",)  # 150 is at the limit, not over it
    assert r.portfolio_headroom == D("500") - D("300.0015")
    # A full 20% position stresses to exactly 3% of equity (U3).
    full = gap_stress({"X": D(1000)}, D(0), denominator=D(5000), scenario_pct=D("0.15"),
                      position_limit_pct=D("0.03"), portfolio_limit_pct=D("0.10"))  # fmt: skip
    assert not full.positions[0].exceeded and full.positions[0].headroom == 0


def test_gap_observation() -> None:
    g = GapObservation("AAPL", D(20), D("100.00"), D("88.50"))
    assert g.pnl == D("-230.00")
    assert g.pct == D("-0.115000")


money = st.decimals(min_value=D("1.00"), max_value=D("100000.00"), places=2)
pcts = st.decimals(min_value=D("0.001"), max_value=D("0.5"), places=3)


@given(money, money, money, pcts)
def test_breach_is_monotonic_in_equity(baseline: Decimal, e1: Decimal, e2: Decimal,
                                       pct: Decimal) -> None:  # fmt: skip
    lower, higher = min(e1, e2), max(e1, e2)
    if is_breached(baseline, higher, pct):
        assert is_breached(baseline, lower, pct)
    assert (
        LimitMeasure(baseline, lower, pct).remaining
        <= LimitMeasure(baseline, higher, pct).remaining
    )


@given(money, money, pcts, pcts)
def test_breach_is_monotonic_in_threshold(baseline: Decimal, equity: Decimal, p1: Decimal,
                                          p2: Decimal) -> None:  # fmt: skip
    tight, loose = min(p1, p2), max(p1, p2)
    if is_breached(baseline, equity, loose):
        assert is_breached(baseline, equity, tight)


@given(money, pcts)
def test_breach_point_is_exactly_the_budget(baseline: Decimal, pct: Decimal) -> None:
    budget = pct * baseline
    assert is_breached(baseline, baseline - budget, pct)
    assert not is_breached(baseline, baseline - budget + D("0.00001"), pct)


@given(st.dictionaries(st.sampled_from(["A", "B", "C", "D"]), money, max_size=4), money, money)
def test_stress_is_additive_and_scales(values: dict[str, Decimal], commitments: Decimal,
                                       denominator: Decimal) -> None:  # fmt: skip
    r = gap_stress(values, commitments, denominator=denominator, scenario_pct=D("0.15"),
                   position_limit_pct=D("0.03"), portfolio_limit_pct=D("0.10"))  # fmt: skip
    assert r.portfolio_stressed == sum(
        (abs(v) * D("0.15") for v in values.values()), D(0)
    ) + commitments * D("0.15")
    assert r.portfolio_stressed >= 0
    assert all(p.limit == denominator * D("0.03") for p in r.positions)
