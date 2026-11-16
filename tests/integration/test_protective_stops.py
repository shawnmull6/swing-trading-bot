"""Step 16.12 (M2-3): durable protective stops through the OMS, against SimBroker.

Offline only: simulate-mode ledgers under ``tmp_path`` (schema version 9) and an
in-memory SimBroker. Policies P1-P8 are the ones approved for simulation (§12.41).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import AcceptThenFail, Reject, SimConfig, SimScriptError
from swing_trading_bot.domain.enums import (
    HaltReason,
    InstrumentType,
    OrderRole,
    OrderStatus,
    OrderType,
    ReconciliationResult,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.trade_plan import (
    ExitAllocationPolicy,
    ExitPhase,
    TradePlan,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
)
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.protection_store import (
    GapCloseReason,
    GapReason,
    PlanStopLevelsRepository,
    ProtectionGap,
    ProtectionGapsRepository,
    StopLevel,
    StopLevelSource,
)
from swing_trading_bot.ledger.repositories import (
    FillsRepository,
    OrdersRepository,
    PositionsRepository,
    ReconciliationRunsRepository,
    TradingStateRepository,
)
from swing_trading_bot.oms.order_manager import (
    CancelOutcome,
    PlacementOutcome,
    PlacementResult,
    PlanExitRequest,
    RefusalCode,
)
from swing_trading_bot.oms.protection import (
    ProtectionState,
    protected_state,
    protection_status,
    record_high_observation,
)
from swing_trading_bot.oms.reconciler import _order_conflicts
from swing_trading_bot.oms.recovery import recover_after_restart
from swing_trading_bot.oms.share_reservations import SellSideBook
from swing_trading_bot.oms.trade_plans import PlanIdentityError, entry_proposal
from swing_trading_bot.risk.gates import (
    ENTRY_ONLY,
    GateFinding,
    GateLayer,
    GateReason,
    GateStatus,
    TradingGates,
)
from tests.support.gates import GATE_CONFIG
from tests.support.oms import OMS_CONFIG, OmsEnv, crash_at, oms_env

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)
SEC5 = timedelta(seconds=5)
SLOW_CANCEL = SimConfig(cancel_delay=timedelta(seconds=30))

INTENT = TradePlanIntent(
    strategy_id="sr-swing",
    strategy_version="1",
    signal_ref="2026-11-16:support-48.10",
    symbol="MSFT",
    instrument_type=InstrumentType.EQUITY,
    entry_limit=D("50.00"),
    protective_stop=D("48.00"),
    partial_target=D("54.00"),
    allocation=ExitAllocationPolicy(D("0.5")),
    trailing=TrailingStopIntent(TrailMode.ABSOLUTE, D("1.50")),
)
PLAN = TradePlan.new(INTENT)


@pytest.fixture
def e(tmp_path: Path) -> OmsEnv:
    return oms_env(tmp_path)


# ---------------------------------------------------------------- helpers


def sync(e: OmsEnv, after: timedelta = MIN, mark: str = "50.00") -> None:
    e.advance(after, marks={"MSFT": mark})
    e.sync.sync(now=e.now())


def open_position(e: OmsEnv, qty: str = "12", *, fill: str | None = None) -> str:
    decision = e.r.evaluate(entry_proposal(PLAN, requested_qty=D(qty)), at=e.now())
    r = e.om.place_entry(decision, now=e.now(), plan=PLAN)
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None, r
    e.sim.schedule_fill(r.client_order_id, D(fill or qty), D("50.00"), after=SEC5)
    sync(e)
    return r.client_order_id


def protect(e: OmsEnv, generation: int = 0) -> PlacementResult:
    return e.om.place_protective_stop(PLAN, generation=generation, now=e.now())


def placed_stop(e: OmsEnv, generation: int = 0) -> str:
    r = protect(e, generation)
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None, r
    return r.client_order_id


def book(e: OmsEnv) -> SellSideBook:
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
        return SellSideBook.build("MSFT", D(0) if pos is None else pos.qty,
                                  OrdersRepository().list_working(s))  # fmt: skip


def gaps(e: OmsEnv) -> list[ProtectionGap]:
    with e.db.read() as s:
        return ProtectionGapsRepository().list_for_plan(s, PLAN.plan_id)


def levels(e: OmsEnv) -> list[StopLevel]:
    with e.db.read() as s:
        return PlanStopLevelsRepository().list_for_plan(s, PLAN.plan_id)


def position(e: OmsEnv) -> Decimal:
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
    return D(0) if pos is None else pos.qty


class StubGates:
    """Returns a fixed gate status (entry-only vs all-order classification tests)."""

    def __init__(self, *reasons: GateReason) -> None:
        self.reasons = reasons

    def evaluate(self, *, now: datetime) -> GateStatus:
        findings = tuple(GateFinding(GateLayer.TRANSIENT, r, "stub") for r in self.reasons)
        return GateStatus(now, RuntimeMode.SIMULATE, True, findings)


# ---------------------------------------------------------------- placement


def test_protective_stop_covers_the_whole_booked_position(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    o = e.order(coid)
    assert (o.side, o.order_type, o.time_in_force, o.qty) == (
        Side.SELL, OrderType.STOP, TimeInForce.GTC, D(12))  # fmt: skip
    assert (o.stop_price, o.limit_price, o.order_role) == (
        D("48.00"),
        None,
        OrderRole.PROTECTIVE_STOP,
    )
    assert o.status is S.ACCEPTED and o.broker_order_id is not None
    assert book(e).reserved == 12 and book(e).available == 0
    (lv,) = levels(e)
    assert (lv.seq, lv.stop_price, lv.source) == (1, D("48.00"), StopLevelSource.PLAN_INTENT)
    assert lv.evidence["client_order_id"] == coid
    assert e.events(coid)[:2] == ["INTENT_CREATED", "SEND_ATTEMPT"]
    assert gaps(e) == []
    retry = protect(e)  # the same step again: reported, never duplicated
    assert retry.refusal is RefusalCode.INTENT_KEY_USED and retry.client_order_id == coid
    assert len(e.submits(coid)) == 1


def test_normal_stop_trigger(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    assert e.sim.market_trade("MSFT", D("48.50")) == []  # above the stop: nothing
    assert e.sim.market_trade("MSFT", D("47.95")) == [coid]
    sync(e, mark="47.95")
    o = e.order(coid)
    assert (o.status, o.filled_qty, o.avg_fill_price) == (S.FILLED, D(12), D("47.95"))
    assert position(e) == 0 and book(e).reserved == 0
    with e.db.read() as s:
        state, _ = protected_state(s, PLAN)
    assert state.phase is ExitPhase.CLOSED
    assert e.r.env.pf.check_invariants(now=e.now()).ok
    assert protect(e, 1).refusal is RefusalCode.NOTHING_TO_PROTECT


# Sessions (fixture calendar): Mon 2026-11-09 14:30-21:00 UTC, Tue 2026-11-10 14:30-21:00.
MON_AFTER_CLOSE = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
TUE_PREMARKET = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
TUE_OPEN = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)


def at(e: OmsEnv, when: datetime, mark: str = "50.00") -> None:
    sync(e, when - e.now(), mark=mark)


def test_gtc_stop_stays_working_overnight_and_gaps_fill_at_the_next_open(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    at(e, MON_AFTER_CLOSE)
    assert e.order(coid).status is S.ACCEPTED  # the close neither cancels nor expires it
    with e.db.read() as s:
        assert protection_status(s, PLAN.plan_id).state is ProtectionState.PROTECTED
    assert e.sim.market_trade("MSFT", D("44.00")) == []  # after-hours print: not eligible
    at(e, TUE_PREMARKET, mark="43.50")
    assert e.sim.market_trade("MSFT", D("43.50")) == []  # pre-market print: not eligible
    assert e.order(coid).status is S.ACCEPTED and e.order(coid).filled_qty == 0
    e.clock.advance_to(TUE_OPEN)
    assert e.sim.market_trade("MSFT", D("44.00")) == [coid]  # first eligible print
    at(e, TUE_OPEN + MIN, mark="44.00")
    o = e.order(coid)
    assert (o.status, o.avg_fill_price) == (S.FILLED, D("44.00"))  # not the 48.00 stop
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
    assert pos is not None and pos.realized_pnl == D(-72)  # 3R; planned risk was 24
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_out_of_hours_stop_submission_and_replacement_are_blocked(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    at(e, MON_AFTER_CLOSE)
    r = protect(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "MARKET_CLOSED" in r.detail
    assert book(e).holds == ()  # nothing created
    at(e, TUE_OPEN + MIN)
    coid = placed_stop(e)  # the next regular session
    assert e.order(coid).status is S.ACCEPTED


def test_sim_stop_simulation_requires_session_boundaries() -> None:
    from swing_trading_bot.broker.sim import SimBroker, SimClock  # noqa: PLC0415

    with pytest.raises(SimScriptError, match="calendar"):
        SimBroker(SimClock(TUE_OPEN)).market_trade("MSFT", D(1))


def test_partial_stop_fill_keeps_the_remainder_held_and_working(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    assert e.sim.market_trade("MSFT", D("47.90"), liquidity=D(5)) == [coid]
    sync(e, mark="47.90")
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.PARTIALLY_FILLED, D(5))
    assert (position(e), book(e).reserved, book(e).available) == (D(7), D(7), D(0))
    assert e.sim.market_trade("MSFT", D("47.80")) == [coid]  # triggered: keeps executing
    sync(e, mark="47.80")
    assert e.order(coid).status is S.FILLED and position(e) == 0


def test_stop_cancellation_records_an_unprotected_interval(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    old = placed_stop(e)
    assert e.om.request_cancel(old, now=e.now()) is CancelOutcome.REQUESTED
    (gap,) = gaps(e)
    assert (gap.reason, gap.prior_client_order_id, gap.open) == (
        GapReason.STOP_CANCEL_REQUESTED, old, True)  # fmt: skip
    assert book(e).reserved == 12  # a cancel request frees nothing
    assert protect(e, 1).refusal is RefusalCode.WORKING_ORDER_EXISTS
    sync(e)  # the broker confirms the cancel
    assert e.order(old).status is S.CANCELED and book(e).reserved == 0
    new = placed_stop(e, 1)
    (gap,) = gaps(e)
    assert (gap.close_reason, gap.closed_by_client_order_id) == (
        GapCloseReason.STOP_ACKNOWLEDGED, new)  # fmt: skip
    assert gap.closed_at is not None and gap.closed_at >= gap.opened_at


def test_declined_cancel_closes_the_interval_on_the_same_stop(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    e.sim.refuse_next_cancel(coid)
    e.om.request_cancel(coid, now=e.now())
    sync(e)
    assert e.order(coid).status is S.ACCEPTED
    e.om.refresh_protection(PLAN.plan_id, now=e.now())
    (gap,) = gaps(e)
    assert (gap.close_reason, gap.closed_by_client_order_id) == (
        GapCloseReason.STOP_ACKNOWLEDGED, coid)  # fmt: skip


def test_late_fill_after_cancellation(tmp_path: Path) -> None:
    """The stop triggers while the cancel is pending, its activity posts late: shares stay
    held, no replacement is possible, and the gap closes as flat once booked."""
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    e.om.request_cancel(coid, now=e.now())
    e.sim.market_trade("MSFT", D("47.90"), activity_delay=timedelta(minutes=5))
    sync(e, mark="47.90")
    o = e.order(coid)
    assert o.status is S.PENDING_CANCEL and o.unbooked_fill_qty == 12
    assert book(e).reserved == 12 and position(e) == 12  # nothing released early
    assert protect(e, 1).refusal in (RefusalCode.GATE_BLOCKED, RefusalCode.WORKING_ORDER_EXISTS)
    sync(e, timedelta(minutes=5), mark="47.90")
    assert e.order(coid).status is S.FILLED and position(e) == 0
    e.om.refresh_protection(PLAN.plan_id, now=e.now())
    (gap,) = gaps(e)
    assert gap.close_reason is GapCloseReason.POSITION_FLAT
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_partial_fill_racing_a_cancel_leaves_a_smaller_position_to_protect(
    tmp_path: Path,
) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    old = placed_stop(e)
    e.om.request_cancel(old, now=e.now())
    e.sim.market_trade("MSFT", D("47.90"), liquidity=D(4))
    sync(e, mark="48.20")
    assert (e.order(old).status, e.order(old).filled_qty) == (S.CANCELED, D(4))
    assert gaps(e)[0].open and position(e) == 8
    new = placed_stop(e, 1)
    assert e.order(new).qty == 8 and e.order(new).stop_price == D("48.00")
    assert not gaps(e)[0].open


def test_stop_that_ends_unfilled_opens_a_gap(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    e.sim.schedule_broker_cancel(coid, after=SEC5)  # e.g. canceled by the broker
    sync(e)
    assert e.order(coid).status is S.CANCELED
    e.om.refresh_protection(PLAN.plan_id, now=e.now())
    (gap,) = gaps(e)
    assert (gap.reason, gap.prior_client_order_id) == (GapReason.STOP_ENDED_UNFILLED, coid)
    e.om.refresh_protection(PLAN.plan_id, now=e.now())  # idempotent
    assert len(gaps(e)) == 1


# ------------------------------------------------------- ambiguity and restart


def test_ambiguous_stop_submission_holds_shares_and_is_adopted(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    r = protect(e)
    assert r.outcome is PlacementOutcome.AMBIGUOUS and r.client_order_id is not None
    coid = r.client_order_id
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS and book(e).reserved == 12
    assert protect(e, 1).refusal is RefusalCode.GATE_BLOCKED  # unresolved order
    e.advance(MIN)
    e.rebuild()
    recover_after_restart(e.recovery, e.sync, now=e.now())
    o = e.order(coid)
    assert o.status is S.ACCEPTED and o.stop_price == D("48.00")
    assert len(e.submits(coid)) == 1  # never resent


def test_restart_and_replay_preserve_stop_identity_and_reservations(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    coid = placed_stop(e)
    before = (e.order(coid), book(e), levels(e))
    e.rebuild()
    after = (e.order(coid), book(e), levels(e))
    assert before == after
    with e.db.read() as s:
        state, stop = protected_state(s, PLAN)
    assert stop == D("48.00") and state.held_qty == 12
    sync(e)  # a broker refresh after the restart matches exactly: no halt
    assert not e.state().halted and e.order(coid).status is S.ACCEPTED


def test_crash_after_stop_intent_then_restart(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.rebuild(crash_at("after_intent_commit"))
    with pytest.raises(BaseException, match="after_intent_commit"):
        protect(e)
    e.rebuild()
    (pending,) = [h for h in book(e).holds if h.status is S.PENDING_SUBMIT]
    assert pending.held_qty == 12 and not e.submits(pending.client_order_id)
    assert e.order(pending.client_order_id).stop_price == D("48.00")  # durable before send
    retry = protect(e)
    assert retry.refusal is RefusalCode.GATE_BLOCKED  # unresolved: never resent


def test_stop_price_mismatch_during_adoption_halts(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.sim.script_submit(AcceptThenFail("read_timeout"))
    coid = protect(e).client_order_id
    assert coid is not None
    e.sim.corrupt_next("get_order_by_client_id", lambda raw: {**raw, "stop_price": "47.00"})
    e.recovery.lookup(coid, now=e.now())
    assert e.state().halted and e.state().reason_code == HaltReason.RECON_ORDER.value
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS  # left untouched
    assert "HALT" in e.events(coid)


def test_reconciliation_flags_a_stop_price_conflict(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    mine = e.order(placed_stop(e))
    snap = e.sim.get_order_by_client_id(mine.client_order_id)
    assert snap is not None
    assert _order_conflicts(mine, snap, is_open=True, lagging=False) == []
    moved = snap.model_copy(update={"stop_price": D("47.00")})
    (finding,) = _order_conflicts(mine, moved, is_open=True, lagging=False)
    assert finding.code == "CONFLICTING_ORDER" and "stop_price" in finding.detail_dict()


# ------------------------------------------------- oversell, sizes, entries


def test_no_other_sell_while_the_stop_holds_every_share(tmp_path: Path) -> None:
    e = oms_env(tmp_path, config=replace(OMS_CONFIG, allow_unprotected_targets=True))
    open_position(e)
    sync(e)
    placed_stop(e)
    for role, qty, price in [(OrderRole.EXIT, "1", "49.00"), (OrderRole.TARGET, "6", "54.00")]:
        req = PlanExitRequest.for_plan(PLAN, role, qty=D(qty), limit_price=D(price),
                                       generation=0)  # fmt: skip
        assert e.om.place_plan_exit(req, now=e.now()).refusal is RefusalCode.WORKING_ORDER_EXISTS
    # The simulated broker refuses an oversell independently (qty available is zero).
    with pytest.raises(Exception, match="insufficient qty"):
        e.sim.submit_order(SubmitOrderRequest("stb-x", "MSFT", Side.SELL, D(1), OrderType.STOP,
                                              TimeInForce.GTC, stop_price=D("47.00")))  # fmt: skip


def test_small_position_gets_whole_position_protection(e: OmsEnv) -> None:
    open_position(e, "1")
    sync(e)
    coid = placed_stop(e)
    assert e.order(coid).qty == 1
    with e.db.read() as s:
        state, _ = protected_state(s, PLAN)
    assert state.phase is ExitPhase.SMALL_POSITION_UNRESOLVED
    assert state.exit_orders().partial_target is None


def test_partial_entry_requires_cancel_and_confirmation_first(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    entry = open_position(e, "12", fill="5")
    assert e.order(entry).status is S.PARTIALLY_FILLED
    r = protect(e)
    assert r.refusal is RefusalCode.ENTRY_STILL_WORKING and "cancel" in r.detail
    assert e.om.request_cancel(entry, now=e.now()) is CancelOutcome.REQUESTED
    assert protect(e).refusal is RefusalCode.ENTRY_STILL_WORKING  # not yet confirmed
    sync(e)
    assert (e.order(entry).status, e.order(entry).filled_qty) == (S.CANCELED, D(5))
    coid = placed_stop(e)
    assert e.order(coid).qty == 5


def test_nothing_to_protect_and_unknown_plans(e: OmsEnv) -> None:
    assert protect(e).refusal is RefusalCode.PLAN_UNKNOWN
    open_position(e, "12", fill="12")
    other = TradePlan(PLAN.plan_id, replace(INTENT, partial_target=D("60.00")))
    r = e.om.place_protective_stop(other, generation=0, now=e.now())
    assert r.refusal is RefusalCode.PLAN_INTENT_MISMATCH
    assert e.om.place_protective_stop(PLAN, generation=-1, now=e.now()).refusal is (
        RefusalCode.INVALID_PLAN_REQUEST
    )


def test_stop_at_or_above_the_market_is_refused(e: OmsEnv) -> None:
    open_position(e)
    sync(e, mark="47.50")
    before = len(e.submits())
    r = protect(e)
    assert r.refusal is RefusalCode.STOP_NOT_BELOW_MARK and len(e.submits()) == before


# --------------------------------------------- disabled, halted, P3 classes


def test_trading_disabled_refuses_protective_stops(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    env = e.r.env
    gates = TradingGates(env.db, env.pf, env.monitor, env.sim,
                         replace(GATE_CONFIG, trading_enabled=False))  # fmt: skip
    gates.run_startup(now=e.now())
    e.om._gates = gates  # test seam
    r = protect(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "TRADING_DISABLED" in r.detail


def test_persistent_halt_refuses_protective_stops(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    with e.db.transaction() as tx:
        TradingStateRepository().halt(tx, HaltReason.WEEKLY_DRAWDOWN, detail="t", actor="t",
                                      at=e.now())  # fmt: skip
    r = protect(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "PERSISTENT_HALT" in r.detail
    e.om._gates = StubGates()  # type: ignore[assignment]  # past the gate: tx check refuses
    assert protect(e).refusal is RefusalCode.HALTED


@pytest.mark.parametrize("reason", sorted(ENTRY_ONLY))
def test_entry_only_restrictions_do_not_block_protection(e: OmsEnv, reason: GateReason) -> None:
    open_position(e)
    sync(e)
    e.om._gates = StubGates(reason)  # type: ignore[assignment]  # test seam
    assert protect(e).outcome is PlacementOutcome.ACCEPTED


@pytest.mark.parametrize(
    "reason",
    [GateReason.RECONCILIATION_MISMATCH, GateReason.BROKER_IDENTITY_MISMATCH,
     GateReason.BROKER_ACCOUNT_RESTRICTED, GateReason.UNRESOLVED_ORDERS,
     GateReason.UNBOOKED_FILLS, GateReason.POSITION_QTY_MISMATCH,
     GateReason.MARKS_STALE_OR_MISSING, GateReason.MARKET_CLOSED, GateReason.TRADING_DISABLED,
     GateReason.PERSISTENT_HALT, GateReason.UNMAPPED_BLOCKER],
)  # fmt: skip
def test_all_order_restrictions_block_protection(e: OmsEnv, reason: GateReason) -> None:
    open_position(e)
    sync(e)
    e.om._gates = StubGates(GateReason.DAILY_LOSS_LOCKOUT, reason)  # type: ignore[assignment]  # test seam
    r = protect(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and r.detail == reason.value


def test_reconciliation_mismatch_refuses_protection(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    with e.db.transaction() as tx:
        ReconciliationRunsRepository().append(tx, ReconciliationResult.MISMATCH, None, e.now())
    r = protect(e)
    assert r.refusal is RefusalCode.GATE_BLOCKED and "RECONCILIATION" in r.detail


def test_risk_exit_follows_p3_but_targets_stay_blocked(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    e.om._gates = StubGates(GateReason.DAILY_LOSS_LOCKOUT)  # type: ignore[assignment]  # test seam
    target = PlanExitRequest.for_plan(PLAN, OrderRole.TARGET, qty=D(6), limit_price=D(54),
                                      generation=0)  # fmt: skip
    assert e.om.place_plan_exit(target, now=e.now()).refusal is RefusalCode.UNPROTECTED_TARGET
    risk_exit = replace(target, role=OrderRole.EXIT, qty=D(12), limit_price=D(49))
    assert e.om.place_plan_exit(risk_exit, now=e.now()).outcome is PlacementOutcome.ACCEPTED


# ----------------------------------------------------- trailing persistence


def test_trailing_state_and_evidence_survive_restart_and_only_ratchet(tmp_path: Path) -> None:
    """Partial bucket sold (offline TARGET opt-in) -> runner: breakeven, then a trailing
    high persisted with its evidence; a restart re-derives the same stop; the stop
    placed afterwards uses it; lower highs never lower it."""
    e = oms_env(tmp_path, config=replace(OMS_CONFIG, allow_unprotected_targets=True))
    open_position(e)
    sync(e)
    target = PlanExitRequest.for_plan(PLAN, OrderRole.TARGET, qty=D(6), limit_price=D(54),
                                      generation=0)  # fmt: skip
    coid = e.om.place_plan_exit(target, now=e.now()).client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(6), D("54.00"), after=SEC5)
    sync(e, mark="54.00")
    bar = {"source": "DAILY_BAR", "feed": "sip", "session": "2026-11-16", "field": "high"}
    with e.db.transaction() as tx:
        lv = record_high_observation(tx, PLAN, high=D("57.00"), evidence=bar, at=e.now())
    assert lv is not None and (lv.stop_price, lv.high_water, lv.source) == (
        D("55.50"), D("57.00"), StopLevelSource.TRAILING)  # fmt: skip
    with e.db.transaction() as tx:  # a lower high changes nothing
        assert record_high_observation(tx, PLAN, high=D("56.00"), evidence=bar,
                                       at=e.now()) is None  # fmt: skip
    with e.db.transaction() as tx, pytest.raises(ValueError, match="evidence"):
        record_high_observation(tx, PLAN, high=D("58.00"), evidence={}, at=e.now())
    with e.db.transaction() as tx, pytest.raises(PersistenceError, match="below"):
        PlanStopLevelsRepository().append(tx, PLAN.plan_id, stop_price=D("50.00"),
                                          high_water=D("57.00"),
                                          source=StopLevelSource.BREAKEVEN, evidence=bar,
                                          at=e.now())  # fmt: skip
    e.rebuild()
    with e.db.read() as s:
        state, stop = protected_state(s, PLAN)
    assert state.phase is ExitPhase.RUNNER and stop == D("55.50") and state.held_qty == 6
    sync(e, mark="56.50")
    new = placed_stop(e)
    assert (e.order(new).qty, e.order(new).stop_price) == (D(6), D("55.50"))
    assert levels(e)[-1].evidence == bar  # no new row: the level was already persisted


# ------------------------------------------------------------ SimBroker


def test_sim_stop_semantics(e: OmsEnv) -> None:
    sim = e.sim
    sim._positions["MSFT"] = (D(10), D(500))  # direct seed for an isolated broker check
    stop = sim.submit_order(SubmitOrderRequest("stb-s1", "MSFT", Side.SELL, D(4), OrderType.STOP,
                                               TimeInForce.GTC, stop_price=D("48.00")))  # fmt: skip
    assert stop.stop_price == D("48.00") and stop.limit_price is None
    sim.submit_order(SubmitOrderRequest("stb-sl", "MSFT", Side.SELL, D(2), OrderType.STOP_LIMIT,
                                        TimeInForce.GTC, limit_price=D("47.00"),
                                        stop_price=D("48.00")))  # fmt: skip
    assert sim.market_trade("AAPL", D("1.00")) == []
    assert sim.market_trade("MSFT", D("48.01")) == []
    assert sim.market_trade("MSFT", D("48.00")) == ["stb-s1"]  # at the stop triggers
    snap = sim.get_order_by_client_id("stb-s1")
    assert snap is not None and snap.broker_status == "filled" and snap.filled_avg_price == D(48)
    sl = sim.get_order_by_client_id("stb-sl")
    assert sl is not None and sl.broker_status == "new"  # STOP_LIMIT is not simulated
    with pytest.raises(SimScriptError):
        sim.market_trade("MSFT", D(0))
    with pytest.raises(ValueError, match="stop"):
        SubmitOrderRequest("stb-s2", "MSFT", Side.SELL, D(1), OrderType.STOP, TimeInForce.GTC)
    with pytest.raises(ValueError, match="stop"):
        SubmitOrderRequest("stb-s3", "MSFT", Side.SELL, D(1), OrderType.LIMIT, TimeInForce.DAY,
                           limit_price=D(50), stop_price=D(48))  # fmt: skip


# ------------------------------------------------- replacement lifecycle (16.12A)


def status(e: OmsEnv) -> ProtectionState:
    with e.db.read() as s:
        return protection_status(s, PLAN.plan_id).state


def raise_level(e: OmsEnv, stop: str = "49.00") -> None:
    """A durable, evidenced higher level (e.g. an approved breakeven move)."""
    with e.db.transaction() as tx:
        PlanStopLevelsRepository().append(tx, PLAN.plan_id, stop_price=D(stop), high_water=None,
                                          source=StopLevelSource.BREAKEVEN,
                                          evidence={"reason": "raise"}, at=e.now())  # fmt: skip


def cancel_confirmed(e: OmsEnv, old: str) -> None:
    assert e.om.request_cancel(old, now=e.now()) is CancelOutcome.REQUESTED
    sync(e)
    assert e.order(old).status is S.CANCELED


def test_replacement_lifecycle_distinguishes_potential_and_confirmed_gaps(
    tmp_path: Path,
) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    assert status(e) is ProtectionState.UNPROTECTED  # held, never protected yet
    old = placed_stop(e)
    assert status(e) is ProtectionState.PROTECTED
    e.om.request_cancel(old, now=e.now())  # cancel requested
    (gap,) = gaps(e)
    assert gap.open and not gap.confirmed  # potential: the old stop may still work
    assert status(e) is ProtectionState.POTENTIAL_GAP
    e.advance(timedelta(seconds=10))
    e.sync.sync(now=e.now())  # cancel still pending at the broker
    assert not gaps(e)[0].confirmed and status(e) is ProtectionState.POTENTIAL_GAP
    sync(e)  # cancel confirmed
    (gap,) = gaps(e)
    assert gap.confirmed and gap.confirmed_at is not None and gap.confirmed_at >= gap.opened_at
    assert status(e) is ProtectionState.UNPROTECTED
    e.sim.script_submit(Reject("rejected for test", 422))
    rejected = protect(e, 1)  # replacement rejected
    assert rejected.outcome is PlacementOutcome.REJECTED
    assert gaps(e)[0].open and status(e) is ProtectionState.UNPROTECTED
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    ambiguous = protect(e, 2)  # replacement ambiguous: never counted as protection
    assert ambiguous.outcome is PlacementOutcome.AMBIGUOUS
    assert gaps(e)[0].open and status(e) is ProtectionState.UNCERTAIN
    e.advance(MIN)
    recover_after_restart(e.recovery, e.sync, now=e.now())  # adopted: acknowledged
    (gap,) = gaps(e)
    assert status(e) is ProtectionState.PROTECTED
    assert (gap.close_reason, gap.closed_by_client_order_id) == (
        GapCloseReason.STOP_ACKNOWLEDGED, ambiguous.client_order_id)  # fmt: skip
    assert gap.closed_at is not None and gap.confirmed_at is not None
    assert gap.closed_at >= gap.confirmed_at


@pytest.mark.parametrize(
    "point",
    ["after_cancel_marker", "after_intent_commit", "send_marker_uncommitted",
     "after_send_marker", "after_submit", "after_outcome_commit"],
)  # fmt: skip
def test_restart_at_each_replacement_transition(tmp_path: Path, point: str) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    old = placed_stop(e)
    raise_level(e)
    if point == "after_cancel_marker":
        e.rebuild(crash_at(point))
        with pytest.raises(BaseException, match=point):
            e.om.request_cancel(old, now=e.now())
    else:
        cancel_confirmed(e, old)
        e.rebuild(crash_at(point))
        with pytest.raises(BaseException, match=point):
            protect(e, 1)
    e.rebuild()  # restart
    e.advance(MIN)
    recover_after_restart(e.recovery, e.sync, now=e.now())
    sync(e)
    assert not e.state().halted, e.state()
    with e.db.read() as s:
        orders = [o for o in OrdersRepository().list_for_plan(s, PLAN.plan_id)
                  if o.order_role is OrderRole.PROTECTIVE_STOP]  # fmt: skip
    for o in orders:  # never a duplicate submission
        assert len(e.submits(o.client_order_id)) <= 1
    assert levels(e)[-1].stop_price == D("49.00")  # the highest durable level is kept
    (gap,) = gaps(e)
    if point == "after_cancel_marker":
        # The cancel never reached the broker: the old stop is still working.
        assert e.order(old).status is S.ACCEPTED and len(orders) == 1
        assert not gap.confirmed and gap.closed_by_client_order_id == old
        assert status(e) is ProtectionState.PROTECTED and book(e).reserved == 12
        return
    (new,) = [o for o in orders if o.client_order_id != old]
    assert new.stop_price == D("49.00") and new.qty == 12  # never lowered
    assert book(e).reserved == 12  # the replacement holds every share in any status
    assert gap.confirmed  # the old stop is confirmed gone
    sent = point in ("after_submit", "after_outcome_commit")
    if sent:  # the broker has it: adopted, acknowledged, gap closed by it
        assert e.order(new.client_order_id).status is S.ACCEPTED
        assert gap.closed_by_client_order_id == new.client_order_id
        assert status(e) is ProtectionState.PROTECTED
    else:  # never sent: unresolved, not protection, gap stays visible
        assert e.order(new.client_order_id).status in (S.PENDING_SUBMIT, S.SUBMIT_AMBIGUOUS)
        assert gap.open and status(e) is ProtectionState.UNCERTAIN
        assert not e.submits(new.client_order_id)
        assert protect(e, 2).refusal is RefusalCode.GATE_BLOCKED  # nothing new meanwhile
    retry = protect(e, 1)  # re-deriving the same step never duplicates it
    assert retry.refusal in (RefusalCode.INTENT_KEY_USED, RefusalCode.GATE_BLOCKED)


def test_late_fill_after_cancel_stays_attributed_across_restart(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SLOW_CANCEL)
    open_position(e)
    sync(e)
    old = placed_stop(e)
    e.om.request_cancel(old, now=e.now())
    e.sim.market_trade("MSFT", D("47.90"), liquidity=D(5), activity_delay=timedelta(minutes=5))
    sync(e, mark="47.90")
    assert e.order(old).unbooked_fill_qty == 5 and book(e).reserved == 12
    assert status(e) is ProtectionState.POTENTIAL_GAP and not gaps(e)[0].confirmed
    e.rebuild()  # restart before the fill posts
    sync(e, timedelta(minutes=5), mark="48.20")
    o = e.order(old)
    assert (o.status, o.filled_qty) == (S.CANCELED, D(5))
    with e.db.read() as s:
        fills = FillsRepository().list_for_order(s, old)
    assert [f.qty for f in fills] == [D(5)]  # booked against the old stop
    assert position(e) == 7 and gaps(e)[0].confirmed and status(e) is ProtectionState.UNPROTECTED
    new = placed_stop(e, 1)
    assert e.order(new).qty == 7 and not gaps(e)[0].open


def test_levels_require_evidence_and_a_verified_plan(e: OmsEnv) -> None:
    open_position(e)
    sync(e)
    with e.db.transaction() as tx, pytest.raises(PersistenceError, match="evidence"):
        PlanStopLevelsRepository().append(tx, PLAN.plan_id, stop_price=D(48), high_water=None,
                                          source=StopLevelSource.PLAN_INTENT, evidence={},
                                          at=e.now())  # fmt: skip
    impostor = TradePlan(PLAN.plan_id, replace(INTENT, protective_stop=D("40.00")))
    with e.db.read() as s, pytest.raises(PlanIdentityError):
        protected_state(s, impostor)  # restart reconstruction verifies the plan record
