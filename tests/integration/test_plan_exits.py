"""M2-3: TradePlan-linked entries and sell-side exits through the OMS, against SimBroker.

Offline only: a simulate-mode ledger under ``tmp_path`` and an in-memory SimBroker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.app import config as app_config
from swing_trading_bot.broker.sim import AcceptThenFail, Reject, SimConfig
from swing_trading_bot.domain.enums import (
    HaltReason,
    InstrumentType,
    OrderRole,
    OrderStatus,
    ReconciliationResult,
)
from swing_trading_bot.domain.trade_plan import (
    ExitAllocationPolicy,
    ExitEvent,
    ExitEventKind,
    ExitPhase,
    TradePlan,
    TradePlanError,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
    new_plan_id,
)
from swing_trading_bot.ledger.portfolio import ConfirmedFill
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    PositionsRepository,
    ReconciliationRunsRepository,
    ReservationsRepository,
    TradingStateRepository,
)
from swing_trading_bot.oms.lifecycle import EventSource, record_event
from swing_trading_bot.oms.order_manager import (
    CancelOutcome,
    OmsConfig,
    PlacementOutcome,
    PlacementResult,
    PlanExitRequest,
    RefusalCode,
)
from swing_trading_bot.oms.recovery import recover_after_restart
from swing_trading_bot.oms.share_reservations import (
    ReplaceReadiness,
    SellSideBook,
    replace_readiness,
)
from swing_trading_bot.oms.trade_plans import (
    PlanIdentityError,
    entry_proposal,
    load_plan,
    rebuild_exit_state,
)
from swing_trading_bot.risk.gates import TradingGates
from tests.support.gates import GATE_CONFIG
from tests.support.oms import OMS_CONFIG, OmsEnv, crash_at, oms_env

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)
SEC5 = timedelta(seconds=5)

INTENT = TradePlanIntent(
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
PLAN = TradePlan.new(INTENT)
TARGET_TESTS = replace(OMS_CONFIG, allow_unprotected_targets=True)
"""Isolated offline OMS tests only: TARGET sells while no protective stop can exist."""


@pytest.fixture
def e(tmp_path: Path) -> OmsEnv:
    return oms_env(tmp_path, config=TARGET_TESTS)


def sync(e: OmsEnv, after: timedelta = MIN, marks: dict[str, str] | None = None) -> None:
    e.advance(after, marks=marks)
    e.sync.sync(now=e.now())


def entry(e: OmsEnv, plan: TradePlan = PLAN, qty: str | None = None) -> PlacementResult:
    proposal = entry_proposal(plan, requested_qty=None if qty is None else D(qty))
    decision = e.r.evaluate(proposal, at=e.now())  # a retry's decision is not approved:
    return e.om.place_entry(decision, now=e.now(), plan=plan)


def place_entry(e: OmsEnv, qty: str | None = None) -> str:
    r = entry(e, qty=qty)
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None, r
    return r.client_order_id


def open_position(e: OmsEnv, qty: str = "12") -> str:
    coid = place_entry(e, qty)
    e.sim.schedule_fill(coid, D(qty), D("50.00"), after=SEC5)
    sync(e)
    assert e.order(coid).status is S.FILLED
    return coid


def exit_req(role: OrderRole = OrderRole.TARGET, qty: str = "6", price: str = "54.00",
             generation: int = 0) -> PlanExitRequest:  # fmt: skip
    return PlanExitRequest.for_plan(PLAN, role, qty=D(qty), limit_price=D(price),
                                    generation=generation)  # fmt: skip


def place_exit(e: OmsEnv, req: PlanExitRequest | None = None) -> PlacementResult:
    return e.om.place_plan_exit(req or exit_req(), now=e.now())


def book(e: OmsEnv) -> SellSideBook:
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
        return SellSideBook.build("MSFT", D(0) if pos is None else pos.qty,
                                  OrdersRepository().list_working(s))  # fmt: skip


# ------------------------------------------------------------------ entries


def test_plan_entry_is_tagged_and_sized_by_the_risk_engine(e: OmsEnv) -> None:
    coid = place_entry(e)
    o = e.order(coid)
    assert (o.trade_plan_id, o.order_role, o.qty) == (PLAN.plan_id, OrderRole.ENTRY, D(12))
    assert o.intent_key == PLAN.entry_intent_key
    with e.db.read() as s:
        assert load_plan(s, PLAN.plan_id) == PLAN  # durable intent copy verifies


def test_plan_entry_with_a_foreign_intent_key_is_refused(e: OmsEnv) -> None:
    decision = e.decide()  # intent key "entry:MSFT:test"
    r = e.om.place_entry(decision, now=e.now(), plan=PLAN)
    assert r.refusal is RefusalCode.PLAN_INTENT_MISMATCH and not e.submits()
    # Same intent key but different prices than the plan: also refused.
    other = replace(entry_proposal(PLAN), entry_price=D("49.00"))
    decision = e.r.evaluate(other, at=e.now())
    r = e.om.place_entry(decision, now=e.now(), plan=PLAN)
    assert r.refusal is RefusalCode.PLAN_INTENT_MISMATCH and "entry_price" in r.detail


# ---------------------------------------------------------- sells and holds


def test_target_holds_shares_and_partial_fills_update_the_hold(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    r = place_exit(e)
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None
    coid = r.client_order_id
    o = e.order(coid)
    assert (o.side.value, o.trade_plan_id, o.order_role) == ("SELL", PLAN.plan_id, OrderRole.TARGET)
    with e.db.read() as s:
        assert ReservationsRepository().get(s, coid) is None  # sells hold shares, not cash
    assert (book(e).reserved, book(e).available) == (D(6), D(6))
    assert "INTENT_CREATED" in e.events(coid)

    e.sim.schedule_fill(coid, D(2), D("54.00"), after=SEC5)
    sync(e)
    assert e.order(coid).status is S.PARTIALLY_FILLED
    assert (book(e).position_qty, book(e).reserved, book(e).available) == (D(10), D(4), D(6))
    e.sim.schedule_fill(coid, D(4), D("54.10"), after=SEC5)
    sync(e)
    assert e.order(coid).status is S.FILLED and book(e).reserved == 0
    assert e.r.env.pf.check_invariants(now=e.now()).ok

    state = _state(e)
    assert state.phase is ExitPhase.RUNNER and state.held_qty == 6
    assert state.stop_price == D("50.00")  # moved to breakeven after the partial
    # Re-deriving the same step (e.g. after a restart) hits the durable intent key and
    # reports the existing order instead of creating a second one.
    again = place_exit(e)
    assert again.refusal is RefusalCode.INTENT_KEY_USED and again.client_order_id == coid


def test_second_sell_while_one_works_is_refused(e: OmsEnv) -> None:
    """Stop/target competition at the OMS: the one-working-order rule refuses it."""
    open_position(e)
    sync(e)
    assert place_exit(e).outcome is PlacementOutcome.ACCEPTED
    r = place_exit(e, exit_req(OrderRole.EXIT, "12", "47.00"))
    assert r.refusal is RefusalCode.WORKING_ORDER_EXISTS


def test_protective_stop_orders_are_refused_until_stop_price_is_persisted(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    r = place_exit(e, exit_req(OrderRole.PROTECTIVE_STOP, "12", "48.00"))
    assert r.refusal is RefusalCode.STOP_ORDERS_UNSUPPORTED


@pytest.mark.parametrize(
    "req",
    [replace(exit_req(), qty=D("1.5")), replace(exit_req(), limit_price=D("54.001")),
     replace(exit_req(), role=OrderRole.ENTRY), replace(exit_req(), trade_plan_id="x"),
     replace(exit_req(), generation=-1)],
)  # fmt: skip
def test_invalid_exit_requests(e: OmsEnv, req: PlanExitRequest) -> None:
    assert place_exit(e, req).refusal is RefusalCode.INVALID_PLAN_REQUEST


def test_oversell_is_refused_and_nothing_is_sent(e: OmsEnv) -> None:
    open_position(e, "5")
    sync(e)
    before = len(e.submits())
    r = place_exit(e, exit_req(OrderRole.EXIT, "6", "50.00"))
    assert r.refusal is RefusalCode.SHARES_UNAVAILABLE and "INSUFFICIENT_SHARES" in r.detail
    assert len(e.submits()) == before


def test_exit_while_entry_still_working_is_refused(e: OmsEnv) -> None:
    coid = place_entry(e)
    e.sim.schedule_fill(coid, D(5), D("50.00"), after=SEC5)
    sync(e)
    assert e.order(coid).status is S.PARTIALLY_FILLED
    assert place_exit(e, exit_req(qty="2")).refusal is RefusalCode.WORKING_ORDER_EXISTS


def test_unknown_plan_is_refused(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    other = replace(exit_req(), trade_plan_id=new_plan_id())
    assert place_exit(e, other).refusal is RefusalCode.PLAN_UNKNOWN


# ---------------------------------------------------------- cancel / replace


def test_late_fill_after_cancellation_keeps_shares_held(tmp_path: Path) -> None:
    e = oms_env(
        tmp_path, config=TARGET_TESTS, sim_config=SimConfig(cancel_delay=timedelta(seconds=30))
    )
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    # 3 shares execute; the activity posts 5 minutes late; then the cancel lands.
    e.sim.schedule_fill(coid, D(3), D("54.00"), after=SEC5, activity_delay=timedelta(minutes=5))
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.REQUESTED
    sync(e)
    o = e.order(coid)
    assert o.status is S.PENDING_CANCEL and o.unbooked_fill_qty == 3  # broker: canceled
    assert replace_readiness(o) is ReplaceReadiness.AWAITING_FILL_BOOKING
    assert book(e).reserved == 6 and book(e).available == 6  # nothing freed yet
    r = place_exit(e, exit_req(generation=1))
    assert r.refusal in (RefusalCode.GATE_BLOCKED, RefusalCode.WORKING_ORDER_EXISTS)

    sync(e, timedelta(minutes=5))
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.CANCELED, D(3))
    assert replace_readiness(o) is ReplaceReadiness.READY
    assert (book(e).position_qty, book(e).reserved) == (D(9), D(0))
    # The replacement is sized from the book now: the 3 remaining partial shares.
    state = _state(e)
    assert state.phase is ExitPhase.PROTECTED and state.partial_remaining == 3
    r = place_exit(e, exit_req(qty=str(state.partial_remaining), generation=1))
    assert r.outcome is PlacementOutcome.ACCEPTED
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_declined_cancel_returns_the_order_to_working(tmp_path: Path) -> None:
    e = oms_env(
        tmp_path, config=TARGET_TESTS, sim_config=SimConfig(cancel_delay=timedelta(seconds=30))
    )
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    e.sim.refuse_next_cancel(coid)
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.REQUESTED
    assert replace_readiness(e.order(coid)) is ReplaceReadiness.AWAITING_CANCEL_CONFIRMATION
    sync(e)
    assert e.order(coid).status is S.ACCEPTED  # the broker declined the cancel
    assert replace_readiness(e.order(coid)) is ReplaceReadiness.CANCEL_NOT_REQUESTED
    assert book(e).reserved == 6
    assert place_exit(e, exit_req(generation=1)).refusal is RefusalCode.WORKING_ORDER_EXISTS


def test_fill_winning_the_cancel_race_means_nothing_to_replace(tmp_path: Path) -> None:
    e = oms_env(
        tmp_path, config=TARGET_TESTS, sim_config=SimConfig(cancel_delay=timedelta(seconds=30))
    )
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(6), D("54.00"), after=SEC5)
    e.om.request_cancel(coid, now=e.now())
    sync(e)
    assert e.order(coid).status is S.FILLED
    assert replace_readiness(e.order(coid)) is ReplaceReadiness.OLD_ORDER_FILLED


# ------------------------------------------------- ambiguity, restart, replay


def test_ambiguous_sell_holds_shares_and_is_adopted(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.sim.script_submit(AcceptThenFail("read_timeout"))
    r = place_exit(e)
    assert r.outcome is PlacementOutcome.AMBIGUOUS and r.client_order_id is not None
    coid = r.client_order_id
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert book(e).reserved == 6 and book(e).uncertain
    assert replace_readiness(e.order(coid)) is ReplaceReadiness.OLD_ORDER_UNRESOLVED
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.NOT_CANCELABLE_LOCALLY
    e.advance(MIN)
    e.rebuild()  # restart
    recover_after_restart(e.recovery, e.sync, now=e.now())
    assert e.order(coid).status is S.ACCEPTED
    assert len(e.submits(coid)) == 1  # never resent


def test_crash_after_intent_then_restart_never_duplicates(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.rebuild(crash_at("after_intent_commit"))
    with pytest.raises(BaseException, match="after_intent_commit"):
        place_exit(e)
    e.rebuild()
    pending = [o for o in book(e).holds if o.status is S.PENDING_SUBMIT]
    assert len(pending) == 1 and pending[0].held_qty == 6  # unsent, still holds shares
    assert not e.submits(pending[0].client_order_id)
    # The unsent order is unresolved: the gate refuses every new order until the §10.3
    # protocol (operator confirmation) resolves it. Nothing is ever resent.
    r = place_exit(e, exit_req(generation=1))
    assert r.refusal is RefusalCode.GATE_BLOCKED and "UNRESOLVED_ORDERS" in r.detail


def test_exit_state_is_rebuilt_from_the_ledger_after_restart(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(6), D("54.00"), after=SEC5)
    sync(e)
    highs = [ExitEvent("bar:1", ExitEventKind.PRICE_HIGH, price=D("57.00"))]
    first = _state(e, highs)
    e.rebuild()
    again = _state(e, highs)
    assert first == again and again.stop_price == D("55.50")
    assert again.phase is ExitPhase.RUNNER


# ---------------------------------------------------------------- gap risk


def test_overnight_gap_through_the_stop(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    state = _state(e)
    gap = state.assess_open(D("44.00"))
    assert gap.stop_breached and gap.r_multiple is not None and gap.r_multiple < -1
    # A risk exit priced at the gap open fills there, far below the stop.
    coid = place_exit(e, exit_req(OrderRole.EXIT, "12", "44.00")).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(12), D("44.00"), after=SEC5)
    sync(e, marks={"MSFT": "44.00"})
    closed = _state(e)
    assert closed.phase is ExitPhase.CLOSED
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
    assert pos is not None and pos.qty == 0 and pos.realized_pnl < -D(24)  # beyond 1R
    assert e.r.env.pf.check_invariants(now=e.now()).ok


# ------------------------------------------ halts, disabled trading, recon


def test_trading_disabled_refuses_exits(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    env = e.r.env
    gates = TradingGates(env.db, env.pf, env.monitor, env.sim,
                         replace(GATE_CONFIG, trading_enabled=False))  # fmt: skip
    gates.run_startup(now=e.now())
    e.om._gates = gates  # test seam
    r = place_exit(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "TRADING_DISABLED" in r.detail


def test_persistent_halt_refuses_exits(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    with e.db.transaction() as tx:
        TradingStateRepository().halt(tx, HaltReason.OPERATOR, detail="test", actor="t",
                                      at=e.now())  # fmt: skip
    r = place_exit(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "PERSISTENT_HALT" in r.detail


def test_reconciliation_mismatch_refuses_exits(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    with e.db.transaction() as tx:
        ReconciliationRunsRepository().append(tx, ReconciliationResult.MISMATCH, None, e.now())
    r = place_exit(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "RECONCILIATION" in r.detail


def test_shares_not_owned_by_the_plan_are_refused(e: OmsEnv) -> None:
    """A non-plan entry for the same symbol makes plan ownership disagree with the
    ledger position: fail closed rather than sell shares the plan does not own."""
    open_position(e, "5")
    sync(e)
    decision = e.decide(qty="2", intent="entry:MSFT:manual")
    coid = e.om.place_entry(decision, now=e.now()).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(2), D("50.00"), after=SEC5)
    sync(e)
    r = place_exit(e, exit_req(OrderRole.EXIT, "5", "49.00"))
    assert r.refusal is RefusalCode.PLAN_POSITION_MISMATCH


def _state(e: OmsEnv, highs: list[ExitEvent] | None = None):  # type: ignore[no-untyped-def]
    with e.db.read() as s:
        return rebuild_exit_state(s, PLAN, highs)


# ------------------------------------------------- unprotected targets (16.11B)


def test_default_oms_refuses_an_unprotected_target(tmp_path: Path) -> None:
    """The integrated workflow (default OmsConfig, as built by app/config.py) never sends
    a TARGET: no protective stop can exist yet. Risk exits stay available."""
    e = oms_env(tmp_path)
    assert e.config.allow_unprotected_targets is False
    open_position(e)
    sync(e)
    before = len(e.submits())
    r = place_exit(e)
    assert r.refusal is RefusalCode.UNPROTECTED_TARGET and len(e.submits()) == before
    assert place_exit(e, exit_req(OrderRole.EXIT, "12", "49.00")).outcome is (
        PlacementOutcome.ACCEPTED
    )


def test_application_oms_config_never_allows_unprotected_targets() -> None:
    source = Path(app_config.__file__).read_text()
    assert "allow_unprotected_targets" not in source
    assert (
        OmsConfig(mode=OMS_CONFIG.mode, ambiguous=OMS_CONFIG.ambiguous).allow_unprotected_targets
        is False
    )


# ---------------------------------------------------------- plan identity (16.11A)


def test_entry_retry_never_creates_a_second_trade(e: OmsEnv) -> None:
    first = place_entry(e)
    retry = entry(e)
    assert retry.refusal is RefusalCode.INTENT_KEY_USED and retry.client_order_id == first
    assert len(e.submits()) == 1
    rebuilt = TradePlan(PLAN.plan_id, INTENT)  # the same trade, rebuilt after a restart
    assert entry(e, rebuilt).client_order_id == first and len(e.submits()) == 1


def test_entry_retry_after_a_crash_reports_the_unsent_order(e: OmsEnv) -> None:
    e.rebuild(crash_at("after_intent_commit"))
    with pytest.raises(BaseException, match="after_intent_commit"):
        entry(e)
    e.rebuild()
    retry = entry(e)
    assert retry.refusal is RefusalCode.INTENT_KEY_USED and retry.status is S.PENDING_SUBMIT
    assert not e.submits()  # never sent, never resent: the §10.3 protocol owns it


def test_independent_trade_with_an_identical_intent_is_not_a_retry(e: OmsEnv) -> None:
    e.sim.script_submit(Reject("test rejection", 422))
    first = entry(e)
    assert first.outcome is PlacementOutcome.REJECTED
    second_plan = TradePlan.new(INTENT)  # a new signal with the same parameters
    assert second_plan.fingerprint == PLAN.fingerprint and second_plan.plan_id != PLAN.plan_id
    second = entry(e, second_plan)
    assert second.outcome is PlacementOutcome.ACCEPTED
    assert second.client_order_id != first.client_order_id
    # ...while a retry of the first trade still reports its own (rejected) order.
    again = entry(e)
    assert again.refusal is RefusalCode.INTENT_KEY_USED and again.status is S.REJECTED
    with e.db.read() as s:
        assert (
            load_plan(s, PLAN.plan_id) == PLAN and load_plan(s, second_plan.plan_id) == second_plan
        )


def test_exit_with_a_different_intent_under_the_same_plan_id_is_refused(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    impostor = TradePlan(PLAN.plan_id, replace(INTENT, partial_target=D("60.00")))
    req = PlanExitRequest.for_plan(impostor, OrderRole.TARGET, qty=D(6), limit_price=D("60.00"),
                                   generation=0)  # fmt: skip
    assert place_exit(e, req).refusal is RefusalCode.PLAN_INTENT_MISMATCH
    bad_fp = replace(exit_req(), plan_fingerprint="tpf-" + "0" * 64)
    assert place_exit(e, bad_fp).refusal is RefusalCode.PLAN_INTENT_MISMATCH
    with e.db.read() as s, pytest.raises(PlanIdentityError):
        rebuild_exit_state(s, impostor)


def test_ambiguous_plan_record_is_refused(e: OmsEnv) -> None:
    coid = open_position(e)
    sync(e)
    with e.db.transaction() as tx:  # a second, conflicting intent copy (append-only log)
        record_event(tx, coid, at=e.now(), source=EventSource.LOCAL, event="PLAN_RECORDED",
                     trade_plan_id=PLAN.plan_id, plan_fingerprint=PLAN.fingerprint,
                     plan_intent=INTENT.canonical())  # fmt: skip
    r = place_exit(e)
    assert r.refusal is RefusalCode.PLAN_IDENTITY_INVALID
    with e.db.read() as s, pytest.raises(TradePlanError):
        load_plan(s, PLAN.plan_id)


def test_plan_is_reloaded_identically_after_restart(e: OmsEnv) -> None:
    open_position(e)
    e.rebuild()
    with e.db.read() as s:
        reloaded = load_plan(s, PLAN.plan_id)
        assert load_plan(s, new_plan_id()) is None
    assert reloaded is not None and reloaded.intent.fingerprint == INTENT.fingerprint
    assert reloaded == PLAN


# ---------------------------------------------------- reservation audit (16.11A)


def test_holds_survive_restart_exactly(tmp_path: Path) -> None:
    e = oms_env(
        tmp_path, config=TARGET_TESTS, sim_config=SimConfig(cancel_delay=timedelta(seconds=30))
    )
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(2), D("54.00"), after=SEC5)
    sync(e)
    e.om.request_cancel(coid, now=e.now())
    before = book(e)
    e.rebuild()
    after = book(e)
    assert before == after and after.reserved == 4 and after.uncertain  # PENDING_CANCEL


def test_late_fill_on_a_terminal_sell_is_booked_then_flagged(tmp_path: Path) -> None:
    """A FILL activity arriving for a sell already CANCELED with all reported fills booked:
    broker truth is booked (position falls), and the sell_share_holds invariant reports
    booked fills above the broker's last filled_qty, so the cycle halts LEDGER_INVARIANT."""
    e = oms_env(
        tmp_path, config=TARGET_TESTS, sim_config=SimConfig(cancel_delay=timedelta(seconds=30))
    )
    open_position(e)
    sync(e)
    coid = place_exit(e).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(3), D("54.00"), after=SEC5)
    e.om.request_cancel(coid, now=e.now())
    sync(e)
    o = e.order(coid)
    assert (o.status, o.filled_qty, o.broker_filled_qty) == (S.CANCELED, D(3), D(3))
    assert e.r.env.pf.check_invariants(now=e.now()).ok
    assert o.broker_order_id is not None
    late = ConfirmedFill(activity_id="20261116000000000::late", client_order_id=coid,
                         broker_order_id=o.broker_order_id, symbol="MSFT", side=o.side,
                         qty=D(1), price=D("54.00"), fees=D(0), transaction_time=e.now(),
                         raw_json="{}")  # fmt: skip
    e.r.env.pf.process_fill(late, now=e.now())
    report = e.r.env.pf.check_invariants(now=e.now())
    assert any(v.invariant == "sell_share_holds" and "booked fills 4" in v.detail
               for v in report.violations)  # fmt: skip
    assert e.state().halted
