"""Support/resistance setup rules and the signal contract (Step 16.14, Phases B-C)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import OrderRole, Side
from swing_trading_bot.domain.money import DECIMAL_CONTEXT
from swing_trading_bot.domain.trade_plan import TradePlan, TrailMode
from swing_trading_bot.oms.trade_plans import entry_proposal
from swing_trading_bot.strategy.market_data import StrategyBar, prepare_series
from swing_trading_bot.strategy.signals import (
    ExitTemplate,
    SetupSignal,
    SignalError,
    SignalResult,
    generate_signal,
    to_trade_plan_intent,
)
from swing_trading_bot.strategy.support_resistance import (
    STRATEGY_ID,
    Confirmation,
    RejectionReason,
    StrategyParamsError,
    SupportResistanceParams,
    average_true_range,
    cluster_zones,
    evaluate_setup,
    round_price_down,
    swing_highs,
    swing_lows,
)
from tests.support.strategy_fixture import (
    CALENDAR,
    OHLC,
    REJECTION_BAR,
    SESSIONS,
    attested,
    bar,
    candle,
    prepared,
    setup_scenario,
)

D = Decimal
P = SupportResistanceParams()
R = RejectionReason


def _bars(rows: list[tuple[str, str, str, str]]) -> list[StrategyBar]:
    return [StrategyBar("X", SESSIONS[i], D(o), D(h), D(lo), D(c), D(1))
            for i, (o, h, lo, c) in enumerate(rows)]  # fmt: skip


def _evaluate(sc_kwargs: dict[str, OHLC] | None = None, params: SupportResistanceParams = P,
              volume: int = 1_000_000) -> SignalResult:  # fmt: skip
    sc = setup_scenario(decision_bar=(sc_kwargs or {}).get("decision_bar", REJECTION_BAR))
    prep = prepared(sc, volume=volume)
    assert prep.series is not None
    return generate_signal(prep.series, CALENDAR, sc.decision_session, params)


# --------------------------------------------------------------------- math
def test_swing_points_need_completed_bars_on_both_sides() -> None:
    lows = ["10", "9", "8", "9", "10", "9", "7"]  # the 7 at the end is NOT a swing low
    bars = _bars([(x, str(D(x) + 1), x, x) for x in lows])
    assert swing_lows(bars, 2) == [(SESSIONS[2], D(8))]
    highs = ["10", "11", "12", "11", "10", "11", "13"]
    hbars = _bars([(x, x, str(D(x) - 1), x) for x in highs])
    assert swing_highs(hbars, 2) == [(SESSIONS[2], D(12))]


def test_equal_adjacent_lows_count_once() -> None:
    bars = _bars([("10", "11", "10", "10"), ("9", "10", "8", "9"), ("9", "10", "8", "9"),
                  ("10", "11", "10", "10"), ("10", "11", "10", "10")])  # fmt: skip
    assert swing_lows(bars, 1) == [(SESSIONS[1], D(8))]


def test_zones_cluster_by_width_and_ignore_input_order() -> None:
    pts = [(SESSIONS[0], D("10.0")), (SESSIONS[1], D("10.4")), (SESSIONS[2], D("11.0")),
           (SESSIONS[3], D("10.2"))]  # fmt: skip
    zones = cluster_zones(pts, D("0.5"))
    assert [(z.floor, z.top, z.touches) for z in zones] == [
        (D("10.0"), D("10.4"), 3), (D("11.0"), D("11.0"), 1)
    ]  # fmt: skip
    assert cluster_zones(list(reversed(pts)), D("0.5")) == zones


def test_average_true_range_includes_gaps() -> None:
    bars = _bars([("10", "11", "9", "10"), ("12", "13", "11.5", "12"), ("12", "12.5", "11", "11")])
    # TR2 = max(1.5, |13-10|, |11.5-10|) = 3 ; TR3 = max(1.5, 0.5, 1) = 1.5
    assert average_true_range(bars, 2) == D("2.25")
    with pytest.raises(StrategyParamsError):
        average_true_range(bars, 3)


def test_round_price_down_respects_the_tick() -> None:
    assert round_price_down(D("49.009")) == D("49.00")
    assert round_price_down(D("0.98765")) == D("0.9876")


# --------------------------------------------------------------------- accepted setup
def test_canonical_setup_is_accepted_with_ordered_prices() -> None:
    res = _evaluate()
    assert res.reasons == ()
    sig = res.signal
    assert sig is not None
    m = res.evaluation.metrics if res.evaluation else None
    assert m is not None and m.support is not None and m.resistance is not None
    assert (m.support.floor, m.support.touches) == (D("47.6000"), 4)
    assert m.resistance.floor == D("56.4000")
    assert sig.entry_limit == D("49.00")
    # stop = floor - 0.25 ATR rounded down; target = resistance - 0.10 ATR rounded down
    assert sig.protective_stop == round_price_down(D("47.6") - D("0.25") * sig.atr)
    assert sig.partial_target == round_price_down(D("56.4") - D("0.10") * sig.atr)
    assert (sig.protective_stop, sig.partial_target) == (D("47.07"), D("56.18"))
    assert sig.protective_stop < sig.entry_limit < sig.partial_target
    assert sig.reward_risk == DECIMAL_CONTEXT.divide(
        sig.partial_target - sig.entry_limit, sig.entry_limit - sig.protective_stop
    )
    assert sig.reward_risk >= P.min_reward_risk


def test_signal_timing_and_identity() -> None:
    sc = setup_scenario()
    res = _evaluate()
    sig = res.signal
    assert sig is not None
    assert sig.decision_session == sc.decision_session
    assert sig.decision_at == CALENDAR.session(sc.decision_session).close_at
    assert sig.entry_session == CALENDAR.next_session(sc.decision_session)
    assert sig.valid_for(sig.entry_session) and not sig.valid_for(sig.decision_session)
    assert sig.signal_ref == f"SRX:{sc.decision_session:%Y%m%d}:{P.fingerprint[:12]}"
    assert sig.strategy_id == STRATEGY_ID
    assert sig.provenance.feed == "sip" and sig.provenance.bars_used == P.required_sessions


def test_signal_translates_into_the_existing_trade_plan_contract() -> None:
    sig = _evaluate().signal
    assert sig is not None
    template = ExitTemplate()
    intent = to_trade_plan_intent(sig, template)
    assert (intent.entry_limit, intent.protective_stop, intent.partial_target) == (
        sig.entry_limit, sig.protective_stop, sig.partial_target
    )  # fmt: skip
    assert intent.signal_ref == sig.signal_ref
    assert intent.allocation.partial_fraction == D("0.5")
    assert intent.trailing.mode is TrailMode.ABSOLUTE
    assert intent.trailing.offset == (2 * sig.atr).quantize(D("0.01"), rounding="ROUND_CEILING")
    plan = TradePlan.new(intent)
    proposal = entry_proposal(plan)
    assert (proposal.side, proposal.entry_price, proposal.stop_price) == (
        Side.BUY, sig.entry_limit, sig.protective_stop
    )  # fmt: skip
    assert proposal.intent_key.endswith(f":{OrderRole.ENTRY.value}:0")
    pct = to_trade_plan_intent(sig, ExitTemplate(trail_atr_multiple=None))
    assert pct.trailing.mode is TrailMode.PERCENT


def test_signal_rejects_bad_price_order() -> None:
    sig = _evaluate().signal
    assert sig is not None
    with pytest.raises(SignalError):
        replace(sig, protective_stop=sig.entry_limit)
    with pytest.raises(SignalError):
        replace(sig, partial_target=sig.entry_limit)
    with pytest.raises(SignalError):
        replace(sig, entry_session=sig.decision_session)


def test_identical_inputs_give_identical_signals() -> None:
    assert _evaluate() == _evaluate()


# --------------------------------------------------------------------- rejections
@pytest.mark.parametrize(
    ("decision_bar", "params", "volume", "expected"),
    [
        # close far above the zone (2 ATR) and already strong: not near support
        ((D("52.00"), D("53.60"), D("51.60"), D("53.20")), P, 1_000_000, R.NOT_NEAR_SUPPORT),
        # bearish bar at support: not confirmed
        ((D("49.00"), D("49.20"), D("47.50"), D("48.20")), P, 1_000_000, R.NOT_CONFIRMED),
        # bullish but never reached the zone top
        ((D("48.50"), D("49.40"), D("48.10"), D("49.20")), P, 1_000_000, R.NOT_CONFIRMED),
        (REJECTION_BAR, replace(P, min_support_touches=9), 1_000_000, R.NO_SUPPORT),
        (REJECTION_BAR, replace(P, min_reward_risk=D("5")), 1_000_000, R.REWARD_RISK_TOO_LOW),
        (REJECTION_BAR, P, 100_000, R.INSUFFICIENT_LIQUIDITY),
        (REJECTION_BAR, replace(P, min_price=D("60")), 1_000_000, R.PRICE_BELOW_MINIMUM),
        (REJECTION_BAR, replace(P, min_atr_pct=D("0.05")), 1_000_000, R.VOLATILITY_TOO_LOW),
        (REJECTION_BAR, replace(P, max_atr_pct=D("0.03")), 1_000_000, R.VOLATILITY_TOO_HIGH),
        (REJECTION_BAR, replace(P, target_buffer_atr=D("4")), 1_000_000,
         R.TARGET_NOT_ABOVE_ENTRY),
    ],
)  # fmt: skip
def test_rejection_reasons_are_explicit(decision_bar, params, volume, expected) -> None:  # type: ignore[no-untyped-def]
    res = _evaluate({"decision_bar": decision_bar}, params, volume)
    assert res.signal is None
    assert expected in res.reasons


def test_no_resistance_above_entry() -> None:
    # a steady decline: support zones exist only below, no swing high above the close
    rows = []
    price = D("80")
    for i in range(70):
        dip = D("1.5") if i % 6 == 3 else D(0)
        o, c = price, price - D("0.3")
        rows.append((o, max(o, c) + D("0.4"), min(o, c) - D("0.4") - dip, c))
        price = c
    i0 = SESSIONS.index(date(2026, 3, 2))
    bars = [bar("DN", SESSIONS[i0 + k], r) for k, r in enumerate(rows)]
    prep = prepare_series("DN", bars, calendar=CALENDAR, corporate_actions=attested("DN"))
    assert prep.series is not None
    res = generate_signal(prep.series, CALENDAR, bars[-1].session_date,
                          replace(P, confirmation=Confirmation.NONE, min_support_touches=1,
                                  max_entry_distance_atr=D("100")))  # fmt: skip
    assert R.NO_RESISTANCE in res.reasons


def test_missing_history_yields_no_signal() -> None:
    sc = setup_scenario()
    prep = prepared(sc, skip=[sc.days[-10]])
    assert prep.series is not None
    res = generate_signal(prep.series, CALENDAR, sc.decision_session, P)
    assert res.signal is None and res.evaluation is None
    assert res.reasons == (R.INSUFFICIENT_HISTORY,)
    assert res.window_issue is not None


def test_reasons_are_reported_in_a_fixed_order() -> None:
    res = _evaluate({"decision_bar": (D("49.00"), D("49.20"), D("47.50"), D("48.20"))},
                    replace(P, min_price=D("60")), 100_000)  # fmt: skip
    assert list(res.reasons) == sorted(res.reasons, key=list(RejectionReason).index)
    assert {R.PRICE_BELOW_MINIMUM, R.INSUFFICIENT_LIQUIDITY, R.NOT_CONFIRMED} <= set(res.reasons)


@pytest.mark.parametrize("mode", list(Confirmation))
def test_confirmation_modes(mode: Confirmation) -> None:
    # the prior bar (50.6667 -> 49.3333) has high 51.0667: close above it, zone tested
    strong = (D("48.20"), D("51.30"), D("47.50"), D("51.20"))
    loose = replace(P, confirmation=mode, max_entry_distance_atr=D("2"), min_reward_risk=D("1"))
    res = _evaluate({"decision_bar": strong}, loose)
    assert res.signal is not None, res.reasons
    weak = (D("48.40"), D("48.50"), D("47.50"), D("47.80"))  # bearish, below prior high
    res2 = _evaluate({"decision_bar": weak}, replace(P, confirmation=mode))
    assert (R.NOT_CONFIRMED in res2.reasons) is (mode is not Confirmation.NONE)


def test_parameter_validation_and_fingerprint() -> None:
    with pytest.raises(StrategyParamsError):
        SupportResistanceParams(pivot_span=0)
    with pytest.raises(StrategyParamsError):
        SupportResistanceParams(level_lookback=5, pivot_span=3)
    with pytest.raises(StrategyParamsError):
        SupportResistanceParams(zone_atr=0.5)  # type: ignore[arg-type]
    with pytest.raises(StrategyParamsError):
        SupportResistanceParams(min_atr_pct=D("0.1"), max_atr_pct=D("0.05"))
    assert SupportResistanceParams().fingerprint == P.fingerprint
    assert replace(P, zone_atr=D("0.6")).fingerprint != P.fingerprint
    assert P.required_sessions == 60


def test_short_window_is_insufficient_history() -> None:
    sc = setup_scenario()
    prep = prepared(sc)
    assert prep.series is not None
    w = prep.series.window(CALENDAR, sc.decision_session, 30).window
    assert w is not None
    assert evaluate_setup(w, P).reasons == (R.INSUFFICIENT_HISTORY,)


# --------------------------------------------------------------------- no leakage
_future_bar = st.tuples(
    st.decimals(min_value=D("1"), max_value=D("200"), places=2),
    st.decimals(min_value=D("0"), max_value=D("20"), places=2),
    st.integers(min_value=0, max_value=10_000_000),
)


@given(st.lists(_future_bar, min_size=1, max_size=15))
def test_future_bars_never_change_a_signal(future: list[tuple[Decimal, Decimal, int]]) -> None:
    """Whatever happens after the decision session cannot change the decision. Future
    paths are continuous (each open = the prior close), so the dataset-level
    corporate-action integrity rule (tested separately) never rejects them."""
    base = setup_scenario()
    rows: list[OHLC] = []
    prev = REJECTION_BAR[3]
    for c, w, _ in future:
        rows.append((prev, max(prev, c) + w, max(D("0.01"), min(prev, c) - w), c))
        prev = c
    sc = setup_scenario(future=rows)
    vols = [v for _, _, v in future]
    full = [*sc.bars()[: len(base.ohlc)],
            *[replace(b, volume=v) for b, v in zip(sc.bars()[len(base.ohlc):], vols,
                                                    strict=True)]]  # fmt: skip
    with_future = prepare_series(
        sc.symbol, full, calendar=CALENDAR, corporate_actions=attested(sc.symbol)
    )
    truncated = prepared(base)
    assert with_future.series is not None and truncated.series is not None
    a = generate_signal(with_future.series, CALENDAR, base.decision_session, P)
    b = generate_signal(truncated.series, CALENDAR, base.decision_session, P)
    assert a.evaluation == b.evaluation
    assert (a.signal is None) == (b.signal is None)
    if a.signal is not None and b.signal is not None:
        assert replace(a.signal, provenance=b.signal.provenance) == b.signal


def test_bar_after_decision_is_ignored_even_if_extreme() -> None:
    crash = (D("20.00"), D("20.50"), D("10.00"), D("11.00"))
    sc = setup_scenario(future=[crash])
    base = setup_scenario()
    assert generate_signal(prepared(sc).series, CALENDAR, base.decision_session, P).evaluation == (  # type: ignore[arg-type]
        generate_signal(prepared(base).series, CALENDAR, base.decision_session, P).evaluation  # type: ignore[arg-type]
    )


def test_strategy_inputs_are_point_in_time_not_candle_from_later_session() -> None:
    sc = setup_scenario(future=[candle(D("49"), D("60"))])
    prep = prepared(sc)
    assert prep.series is not None
    res = generate_signal(prep.series, CALENDAR, sc.decision_session, P)
    assert res.signal is not None
    assert res.evaluation is not None and res.evaluation.metrics.close == REJECTION_BAR[3]
    assert isinstance(res.signal, SetupSignal)
