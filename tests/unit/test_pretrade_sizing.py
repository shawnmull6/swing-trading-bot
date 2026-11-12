"""Step 11 properties: an approved quantity never exceeds any sizing constraint.

Each constraint is re-derived here independently of ``risk.pretrade.size``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swing_trading_bot.config.fees import load_fee_model
from swing_trading_bot.config.risk_limits import load_risk_limits
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.money import reservation_amount
from swing_trading_bot.risk.pretrade import (
    Constraint,
    SizingInputs,
    max_whole_qty,
    size,
)
from tests.support.pretrade import RiskEnv, proposal, risk_env

ROOT = Path(__file__).resolve().parents[2]
LIMITS = load_risk_limits(ROOT / "config" / "risk.yaml")
FEES = load_fee_model(ROOT / "config" / "fees.yaml")
D = Decimal


def cents(lo: str, hi: str) -> st.SearchStrategy[Decimal]:
    return st.decimals(D(lo), D(hi), places=2, allow_nan=False, allow_infinity=False)


@st.composite
def sizing_inputs(draw: st.DrawFn) -> SizingInputs:
    entry = draw(cents("10.00", "2000.00"))
    stop = draw(cents("0.01", str(entry - D("0.01"))))
    return SizingInputs(
        entry=entry,
        stop=stop,
        equity=draw(cents("100.00", "20000.00")),
        baseline_equity=draw(cents("100.00", "20000.00")),
        held_market_value=draw(cents("0", "3000.00")),
        pending_symbol_commitment=draw(cents("0", "1000.00")),
        capital_headroom=draw(cents("-500.00", "5000.00")),
        available_cash=draw(cents("-500.00", "6000.00")),
        portfolio_stressed=draw(cents("0", "1000.00")),
        broker_cash=draw(cents("0", "100000.00")),
    )


fee_models = st.builds(
    FeeModel,
    cents("0", "5.00"),
    st.decimals(D(0), D("0.05"), places=4),
    st.decimals(D(0), D("0.001"), places=6),
    cents("0", "10.00"),
    st.decimals(D(0), D("0.0001"), places=7),
)


def violated(q: Decimal, i: SizingInputs, fees: FeeModel) -> set[Constraint]:
    """Every constraint quantity ``q`` breaks, computed from first principles."""
    gap = LIMITS.gap_stress
    exposure = i.held_market_value + i.pending_symbol_commitment
    cost = q * i.entry + fees.buy_fee(q)
    reserve = reservation_amount(cost)
    risk = q * (i.entry - i.stop) + fees.buy_fee(q) + fees.sell_fee(q, i.stop)
    checks = {
        Constraint.RISK_BUDGET: risk <= LIMITS.planned_risk_pct_equity * i.equity,
        Constraint.POSITION_CONCENTRATION: exposure + q * i.entry
        <= LIMITS.max_position_pct_equity * i.equity,
        Constraint.ORDER_COMMITMENT: reserve <= LIMITS.max_order_notional,
        Constraint.CAPITAL_CEILING: reserve <= i.capital_headroom,
        Constraint.AVAILABLE_CASH: reserve <= i.available_cash,
        Constraint.GAP_STRESS_POSITION: (exposure + q * i.entry) * gap.scenario_gap_pct
        <= gap.max_position_scenario_loss_pct_equity * i.baseline_equity,
        Constraint.GAP_STRESS_PORTFOLIO: i.portfolio_stressed + q * i.entry * gap.scenario_gap_pct
        <= gap.max_portfolio_scenario_loss_pct_equity * i.baseline_equity,
        Constraint.BROKER_CASH_BACKSTOP: cost <= i.broker_cash,
    }
    return {name for name, ok in checks.items() if not ok}


@given(sizing_inputs(), fee_models)
def test_max_qty_satisfies_every_constraint_and_is_maximal(i: SizingInputs, fees: FeeModel) -> None:
    preview = size(i, LIMITS, fees)
    q = preview.max_qty
    assert q >= 0
    assert q == min(c.max_qty for c in preview.caps)
    if q > 0:
        assert violated(D(q), i, fees) == set()  # never above any constraint
    assert violated(D(q + 1), i, fees)  # one more share always breaks something
    for cap in preview.caps:  # each cap is exactly that constraint's maximum
        if cap.max_qty > 0:
            assert cap.constraint not in violated(D(cap.max_qty), i, fees)
        assert cap.constraint in violated(D(cap.max_qty + 1), i, fees)
    assert set(preview.binding) <= violated(D(q + 1), i, fees)


@given(st.integers(0, 10_000), st.integers(-50, 50))
def test_max_whole_qty_never_rounds_up(true_max: int, estimate_error: int) -> None:
    assert max_whole_qty(lambda q: q <= true_max, true_max + estimate_error) == true_max


def test_max_whole_qty_with_nothing_allowed() -> None:
    assert max_whole_qty(lambda q: q <= -1, 5) == 0


# ------------------------------------------------- engine-level property


@pytest.fixture(scope="module")
def shared_env(tmp_path_factory: pytest.TempPathFactory) -> RiskEnv:
    return risk_env(tmp_path_factory.mktemp("pretrade-prop"))


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    entry=cents("10.00", "600.00"),
    stop_fraction=st.decimals(D("0.50"), D("0.999"), places=3),
    requested=st.none() | st.integers(1, 120),
)
def test_engine_approvals_respect_every_limit(
    shared_env: RiskEnv, entry: Decimal, stop_fraction: Decimal, requested: int | None
) -> None:
    stop = (entry * stop_fraction).quantize(D("0.01"))
    if stop >= entry or stop <= 0:
        return
    shared_env.set_marks({"PROP": str(entry)})
    qty = None if requested is None else str(requested)
    d = shared_env.evaluate(proposal("PROP", str(entry), str(stop), qty=qty))
    if not d.approved:
        assert d.approved_qty == 0 and d.reservation == 0 and d.rejections
        return
    q = d.approved_qty
    assert d.sizing is not None and q <= d.sizing.max_qty
    if requested is not None:
        assert q == requested  # never silently resized
    equity = d.evidence.equity
    assert equity is not None
    assert d.planned_risk <= LIMITS.planned_risk_pct_equity * equity
    assert d.reservation <= LIMITS.max_order_notional
    assert q * entry <= LIMITS.max_position_pct_equity * equity
    assert d.evidence.available_cash is not None and d.reservation <= d.evidence.available_cash
    assert q * entry * LIMITS.gap_stress.scenario_gap_pct <= (
        LIMITS.gap_stress.max_position_scenario_loss_pct_equity * equity
    )
