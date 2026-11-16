"""M2-3: the pure TradePlan contract, whole-share allocation and the exit-state reducer."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import InstrumentType, OrderRole
from swing_trading_bot.domain.ids import uuid7
from swing_trading_bot.domain.trade_plan import (
    ExitAllocationPolicy,
    ExitEvent,
    ExitEventKind,
    ExitPhase,
    ExitState,
    ExitStateError,
    TradePlan,
    TradePlanError,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
    allocate_exit_quantities,
    exit_intent_key,
    is_fingerprint,
    is_plan_id,
    new_plan_id,
    replay,
)

D = Decimal
K = ExitEventKind


def intent(**changes: object) -> TradePlanIntent:
    base = TradePlanIntent(
        strategy_id="sr-swing",
        strategy_version="1",
        signal_ref="2026-11-13:support-48.10",
        symbol="MSFT",
        instrument_type=InstrumentType.EQUITY,
        entry_limit=D("50.00"),
        protective_stop=D("48.00"),
        partial_target=D("54.00"),
        allocation=ExitAllocationPolicy(D("0.5")),
        trailing=TrailingStopIntent(TrailMode.ABSOLUTE, D("1.50")),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def ev(event_id: str, kind: K, qty: str = "0", price: str = "0") -> ExitEvent:
    return ExitEvent(event_id, kind, D(qty), D(price))


def opened(qty: str = "12", price: str = "50.00", **changes: object) -> ExitState:
    return replay(intent(**changes), [ev("f1", K.ENTRY_FILL, qty, price),
                                      ev("closed", K.ENTRY_CLOSED)])  # fmt: skip


# ----------------------------------------------------------------- intent


def test_fingerprint_is_deterministic_content_hash() -> None:
    a, b = intent(), intent()
    assert a.fingerprint == b.fingerprint and is_fingerprint(a.fingerprint)
    assert intent(signal_ref="other").fingerprint != a.fingerprint
    assert intent(partial_target=D("55.00")).fingerprint != a.fingerprint
    assert intent(breakeven_after_partial=False).fingerprint != a.fingerprint


def test_independent_trades_with_identical_intents_get_distinct_ids() -> None:
    p1, p2 = TradePlan.new(intent()), TradePlan.new(intent())
    assert p1.plan_id != p2.plan_id and is_plan_id(p1.plan_id) and is_plan_id(p2.plan_id)
    assert p1.fingerprint == p2.fingerprint  # same intent, different trades
    assert p1.entry_intent_key != p2.entry_intent_key
    assert p1.entry_intent_key == f"{p1.plan_id}:ENTRY:0"


def test_a_retry_reuses_the_same_identity() -> None:
    p = TradePlan.new(intent())
    retry = TradePlan(p.plan_id, intent())  # e.g. rebuilt after a restart
    assert retry == p and retry.entry_intent_key == p.entry_intent_key
    assert exit_intent_key(p.plan_id, OrderRole.TARGET, 2) == f"{p.plan_id}:TARGET:2"


@pytest.mark.parametrize(
    "bad",
    ["plan-1", "tp-" + "a" * 32,  # not a UUIDv7 (version nibble)
     "tp-" + uuid7().hex.upper(), "tp-" + uuid4().hex, "tpf-" + "0" * 64],
)  # fmt: skip
def test_plan_ids_must_be_uuid7(bad: str) -> None:
    assert not is_plan_id(bad)
    with pytest.raises(TradePlanError):
        TradePlan(bad, intent())
    with pytest.raises(TradePlanError):
        exit_intent_key(bad, OrderRole.TARGET, 0)


def test_new_plan_id_uses_the_injected_generator() -> None:
    fixed = "tp-" + uuid7().hex
    assert TradePlan.new(intent(), new_id=lambda: fixed).plan_id == fixed
    with pytest.raises(TradePlanError):
        exit_intent_key(new_plan_id(), OrderRole.TARGET, -1)


def test_canonical_round_trip_and_tampering() -> None:
    i = intent(trailing=TrailingStopIntent(TrailMode.PERCENT, D("0.03")))
    assert TradePlanIntent.from_canonical(i.canonical()) == i
    assert TradePlanIntent.from_canonical(json.loads(json.dumps(i.canonical()))) == i
    good = i.canonical()
    tampered: list[dict[str, object]] = [
        {k: v for k, v in good.items() if k != "symbol"},
        {**good, "extra": "x"},
        {**good, "entry_limit": 50.0},
        {**good, "breakeven_after_partial": "true"},
        {**good, "entry_limit": "abc"},
        {**good, "protective_stop": "60.00"},
        {**good, "trail_mode": "SIDEWAYS"},
        {**good, "min_partial_qty": "1.5"},
    ]
    for data in tampered:
        with pytest.raises(TradePlanError):
            TradePlanIntent.from_canonical(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"protective_stop": D("50.00")},  # stop not below entry
        {"partial_target": D("50.00")},  # target not above entry
        {"entry_limit": D("50.001")},  # sub-penny above $1
        {"protective_stop": D("-1")},
        {"symbol": "msft"},
        {"strategy_id": ""},
        {"signal_ref": "has space"},
    ],
)
def test_invalid_intents_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(TradePlanError):
        intent(**changes)


def test_intent_is_immutable() -> None:
    with pytest.raises(AttributeError):
        intent().entry_limit = D(1)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mode", "offset"),
    [(TrailMode.PERCENT, D(0)), (TrailMode.PERCENT, D(1)), (TrailMode.ABSOLUTE, D(0)),
     (TrailMode.ABSOLUTE, D("0.005")), (TrailMode.ABSOLUTE, D("NaN"))],
)  # fmt: skip
def test_invalid_trailing_specs(mode: TrailMode, offset: Decimal) -> None:
    with pytest.raises(TradePlanError):
        TrailingStopIntent(mode, offset)


def test_trailing_stop_rounds_down_to_a_valid_cent() -> None:
    assert TrailingStopIntent(TrailMode.PERCENT, D("0.03")).stop_for(D("57.33")) == D("55.61")
    assert TrailingStopIntent(TrailMode.ABSOLUTE, D("1.50")).stop_for(D("57.33")) == D("55.83")
    assert TrailingStopIntent(TrailMode.ABSOLUTE, D("1.50")).stop_for(D("1.00")) is None


def test_allocation_policy_validation() -> None:
    for frac in (D(0), D(1), D("1.5")):
        with pytest.raises(TradePlanError):
            ExitAllocationPolicy(frac)
    with pytest.raises(TradePlanError):
        ExitAllocationPolicy(D("0.5"), min_partial_qty=0)
    with pytest.raises(TradePlanError):
        ExitAllocationPolicy(0.5)  # type: ignore[arg-type]  # floats never slip in


# -------------------------------------------------------------- allocation


@pytest.mark.parametrize(
    ("qty", "partial", "runner", "feasible"),
    [("12", "6", "6", True), ("3", "1", "2", True), ("2", "1", "1", True),
     ("1", "0", "1", False), ("0", "0", "0", False)],
)  # fmt: skip
def test_small_positions(qty: str, partial: str, runner: str, feasible: bool) -> None:
    a = allocate_exit_quantities(D(qty), ExitAllocationPolicy(D("0.5")))
    assert (a.partial_qty, a.runner_qty, a.feasible) == (D(partial), D(runner), feasible)
    if not feasible:
        assert a.reason is not None


def test_allocation_never_rounds_up_and_respects_minimums() -> None:
    p = ExitAllocationPolicy(D("0.33"), min_partial_qty=2, min_runner_qty=2)
    assert allocate_exit_quantities(D(5), p).feasible is False  # floor(1.65)=1 < 2
    a = allocate_exit_quantities(D(7), p)  # floor(2.31)=2, runner 5
    assert (a.partial_qty, a.runner_qty) == (D(2), D(5))
    with pytest.raises(TradePlanError):
        allocate_exit_quantities(D("2.5"), p)


@given(
    total=st.integers(min_value=0, max_value=10_000),
    frac=st.decimals(min_value=D("0.01"), max_value=D("0.99"), places=2),
    min_p=st.integers(min_value=1, max_value=5),
    min_r=st.integers(min_value=1, max_value=5),
)
def test_allocation_properties(total: int, frac: Decimal, min_p: int, min_r: int) -> None:
    a = allocate_exit_quantities(D(total), ExitAllocationPolicy(frac, min_p, min_r))
    assert a.partial_qty + a.runner_qty == D(total)
    assert a.partial_qty <= D(total) * frac  # never rounded up
    if a.feasible:
        assert a.partial_qty >= min_p and a.runner_qty >= min_r
    else:
        assert a.partial_qty == 0


# ------------------------------------------------------------ exit state


def test_full_lifecycle_partial_breakeven_trail_close() -> None:
    s = ExitState.start(intent())
    s = s.apply(ev("f1", K.ENTRY_FILL, "5", "49.98"))
    assert s.phase is ExitPhase.ENTRY_PARTIAL
    s = s.apply(ev("f2", K.ENTRY_FILL, "7", "50.00")).apply(ev("closed", K.ENTRY_CLOSED))
    assert s.phase is ExitPhase.PROTECTED and s.held_qty == 12
    assert s.allocation is not None and s.allocation.partial_qty == 6
    assert s.stop_price == D("48.00") and s.breakeven == D("50.00")  # 49.9916.. rounded up
    s = s.apply(ev("h1", K.PRICE_HIGH, price="53.00"))
    assert s.stop_price == D("48.00")  # no trailing before the partial is taken
    s = s.apply(ev("t1", K.TARGET_FILL, "4", "54.00"))
    assert s.phase is ExitPhase.PROTECTED and s.partial_remaining == 2  # partly taken
    s = s.apply(ev("t2", K.TARGET_FILL, "2", "54.05"))
    assert s.phase is ExitPhase.RUNNER and s.held_qty == 6
    assert s.stop_price == D("51.50")  # max(breakeven 50.00, trail 53.00 - 1.50)
    s = s.apply(ev("h2", K.PRICE_HIGH, price="56.00"))
    assert s.stop_price == D("54.50")
    s = s.apply(ev("h3", K.PRICE_HIGH, price="52.00"))
    assert s.stop_price == D("54.50") and s.high_water == D("56.00")  # never lowered
    s = s.apply(ev("s1", K.STOP_FILL, "6", "54.48"))
    assert s.phase is ExitPhase.CLOSED and s.held_qty == 0
    assert s.exit_orders().protective_stop is None


def test_breakeven_disabled_keeps_initial_stop_until_trail_exceeds_it() -> None:
    s = opened(breakeven_after_partial=False).apply(ev("t", K.TARGET_FILL, "6", "54.00"))
    assert s.phase is ExitPhase.RUNNER and s.stop_price == D("48.00")


def test_stop_and_target_compete_for_the_same_shares() -> None:
    plan = opened().exit_orders()
    assert plan.protective_stop is not None and plan.protective_stop.qty == 12
    assert plan.partial_target is not None and plan.partial_target.qty == 6
    assert plan.share_conflict and plan.policy_required is not None
    assert "STOP_TARGET_SHARE_CONFLICT" in plan.policy_required
    runner = opened().apply(ev("t", K.TARGET_FILL, "6", "54.00")).exit_orders()
    assert runner.partial_target is None and not runner.share_conflict


def test_small_position_enters_unresolved_phase_but_keeps_stop() -> None:
    s = opened("1")
    assert s.phase is ExitPhase.SMALL_POSITION_UNRESOLVED
    plan = s.exit_orders()
    assert plan.partial_target is None and plan.protective_stop is not None
    assert plan.policy_required is not None and "SMALL_POSITION" in plan.policy_required
    with pytest.raises(ExitStateError):
        s.apply(ev("t", K.TARGET_FILL, "1", "54.00"))  # no partial bucket exists
    assert s.apply(ev("s", K.STOP_FILL, "1", "47.90")).phase is ExitPhase.CLOSED


def test_entry_without_fills_is_abandoned() -> None:
    s = replay(intent(), [ev("closed", K.ENTRY_CLOSED)])
    assert s.phase is ExitPhase.ENTRY_ABANDONED
    assert s.apply(ev("h", K.PRICE_HIGH, price="60")).phase is ExitPhase.ENTRY_ABANDONED


@pytest.mark.parametrize(
    "bad",
    [ev("x", K.STOP_FILL, "13", "50"),  # oversell
     ev("x", K.TARGET_FILL, "7", "54"),  # beyond the partial bucket
     ev("x", K.ENTRY_FILL, "1", "50")],  # entry fill after the entry closed
)  # fmt: skip
def test_impossible_events_raise(bad: ExitEvent) -> None:
    with pytest.raises(ExitStateError):
        opened().apply(bad)


def test_sell_before_entry_closed_raises() -> None:
    s = ExitState.start(intent()).apply(ev("f1", K.ENTRY_FILL, "5", "50"))
    with pytest.raises(ExitStateError):
        s.apply(ev("s", K.STOP_FILL, "1", "49"))


def test_replay_is_idempotent_and_conflicting_replay_raises() -> None:
    events = [
        ev("f1", K.ENTRY_FILL, "12", "50.00"),
        ev("closed", K.ENTRY_CLOSED),
        ev("t", K.TARGET_FILL, "6", "54.00"),
        ev("h", K.PRICE_HIGH, price="57.00"),
    ]
    once = replay(intent(), events)
    twice = replay(intent(), events + events)
    assert once == twice
    with pytest.raises(ExitStateError):
        once.apply(ev("t", K.TARGET_FILL, "5", "54.00"))


def test_overnight_gap_through_the_stop() -> None:
    s = opened()
    g = s.assess_open(D("44.00"))
    assert g.stop_breached and g.stop_price == D("48.00")
    assert g.planned_loss == D(24) and g.loss_at_open == D(72)
    assert g.r_multiple == D(-3)  # a loss three times the planned risk
    assert not s.assess_open(D("49.00")).stop_breached
    with pytest.raises(TradePlanError):
        ExitState.start(intent()).assess_open(D(40))


def test_event_validation() -> None:
    with pytest.raises(TradePlanError):
        ExitEvent("", K.PRICE_HIGH, price=D(1))
    with pytest.raises(TradePlanError):
        ExitEvent("a", K.ENTRY_FILL, D("1.5"), D(50))
    with pytest.raises(TradePlanError):
        ExitEvent("a", K.PRICE_HIGH)


@given(highs=st.lists(st.integers(min_value=4000, max_value=9000), min_size=1, max_size=40))
def test_runner_stop_never_decreases(highs: list[int]) -> None:
    s = opened().apply(ev("t", K.TARGET_FILL, "6", "54.00"))
    last = s.stop_price
    assert last is not None
    for i, cents in enumerate(highs):
        s = s.apply(ev(f"h{i}", K.PRICE_HIGH, price=str(D(cents) / 100)))
        assert s.stop_price is not None and s.stop_price >= last
        last = s.stop_price
    assert last >= D("50.00")  # never below breakeven once the partial is taken
