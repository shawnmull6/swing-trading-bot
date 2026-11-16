"""Step 16.13 (M2-3): exit orchestration end to end, against SimBroker.

Offline only: simulate-mode ledgers under ``tmp_path`` and an in-memory SimBroker. The
integrated default never sends a TARGET; the touch-triggered partial exit is exercised
only with an explicit, simulation-only opt-in (``TOUCH``).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.broker.sim import AcceptThenFail, Reject, SimConfig
from swing_trading_bot.config.operating_policy import load_operating_policy
from swing_trading_bot.config.risk_limits import load_risk_limits
from swing_trading_bot.config.yaml_loader import ConfigFileError
from swing_trading_bot.domain.enums import (
    HaltReason,
    InstrumentType,
    OrderRole,
    OrderStatus,
    ReconciliationResult,
    RuntimeMode,
)
from swing_trading_bot.domain.trade_plan import (
    ExitAllocationPolicy,
    ExitPhase,
    TradePlan,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
)
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.portfolio import market_date
from swing_trading_bot.ledger.protection_store import (
    PlanStopLevelsRepository,
    ProtectionGap,
    ProtectionGapsRepository,
    StopLevelSource,
)
from swing_trading_bot.ledger.repositories import (
    OrderEventsRepository,
    OrderRecord,
    OrdersRepository,
    PositionsRepository,
    ReconciliationRunsRepository,
    TradingStateRepository,
)
from swing_trading_bot.oms.exit_orchestrator import (
    ORCHESTRATOR_ACTOR,
    PROTECTION_BREACHED_EVENT,
    ExitAction,
    ExitOrchestrator,
    ExitPolicy,
    PriceEvidence,
    StepResult,
    evidence_problems,
)
from swing_trading_bot.oms.order_manager import PlacementOutcome
from swing_trading_bot.oms.protection import (
    ProtectionState,
    protected_state,
    record_high_observation,
)
from swing_trading_bot.oms.reconciler import resume_after_clean_reconciliation
from swing_trading_bot.oms.recovery import recover_after_restart
from swing_trading_bot.oms.trade_plans import entry_proposal
from tests.support.oms import OMS_CONFIG, OmsEnv, crash_at, oms_env

D = Decimal
S = OrderStatus
A = ExitAction
P = ProtectionState
MIN = timedelta(minutes=1)
SEC5 = timedelta(seconds=5)
SLOW_CANCEL = SimConfig(cancel_delay=timedelta(seconds=30))
DEFAULT = ExitPolicy()
TOUCH = ExitPolicy(touch_partial_exit=True)
TARGETS_OK = replace(OMS_CONFIG, allow_unprotected_targets=True)
BREACHED = PROTECTION_BREACHED_EVENT
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
MON_AFTER_CLOSE = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
TUE_OPEN = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)

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


# ---------------------------------------------------------------- helpers


def env(tmp_path: Path, *, touch: bool = False, sim: SimConfig | None = None) -> OmsEnv:
    return oms_env(tmp_path, sim_config=sim, config=TARGETS_OK if touch else OMS_CONFIG)


def orch(e: OmsEnv, policy: ExitPolicy = DEFAULT) -> ExitOrchestrator:
    return ExitOrchestrator(e.db, e.om, e.r.env.gates, policy)


def ev(e: OmsEnv, last: str, high: str | None = None, *, age: timedelta = timedelta(0),
       **changes: object) -> PriceEvidence:  # fmt: skip
    as_of = e.now() - age
    hi = high or last
    oid = f"sim-{as_of:%Y%m%dT%H%M%S}-{last}-{hi}"
    base = PriceEvidence("MSFT", D(last), D(hi), as_of, "SIM", "sim", market_date(as_of), oid)
    return replace(base, **changes)  # type: ignore[arg-type]


def step(e: OmsEnv, policy: ExitPolicy = DEFAULT, evidence: PriceEvidence | None = None
         ) -> StepResult:  # fmt: skip
    return orch(e, policy).step(PLAN, now=e.now(), evidence=evidence)


def sync(e: OmsEnv, after: timedelta = MIN, mark: str = "50.00") -> None:
    e.advance(after, marks={"MSFT": mark})
    e.sync.sync(now=e.now())


def enter(e: OmsEnv, qty: str = "12", fill: str | None = None) -> str:
    decision = e.r.evaluate(entry_proposal(PLAN, requested_qty=D(qty)), at=e.now())
    r = e.om.place_entry(decision, now=e.now(), plan=PLAN)
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None, r
    assert step(e).action is A.WAIT_ENTRY
    e.sim.schedule_fill(r.client_order_id, D(fill or qty), D("50.00"), after=SEC5)
    sync(e)
    return r.client_order_id


def protected(e: OmsEnv, policy: ExitPolicy = DEFAULT, qty: str = "12") -> str:
    enter(e, qty)
    r = step(e, policy)
    assert r.action is A.PLACE_STOP and r.protection is P.PROTECTED, r
    assert r.client_order_id is not None
    return r.client_order_id


def plan_orders(e: OmsEnv, role: OrderRole | None = None) -> list[OrderRecord]:
    with e.db.read() as s:
        orders = OrdersRepository().list_for_plan(s, PLAN.plan_id)
    return [o for o in orders if role is None or o.order_role is role]


def gaps(e: OmsEnv) -> list[ProtectionGap]:
    with e.db.read() as s:
        return ProtectionGapsRepository().list_for_plan(s, PLAN.plan_id)


def realized(e: OmsEnv) -> Decimal:
    with e.db.read() as s:
        pos = PositionsRepository().get(s, 1, "MSFT")
    assert pos is not None
    return pos.realized_pnl


def one_submit_per_order(e: OmsEnv) -> None:
    for o in plan_orders(e):
        assert len(e.submits(o.client_order_id)) <= 1, o.client_order_id


# ------------------------------------------------------------ full lifecycle


def test_full_lifecycle_entry_stop_partial_breakeven_trailing_exit(tmp_path: Path) -> None:
    """Simulation-only opt-in: touch-triggered partial exit with recorded gaps."""
    e = env(tmp_path, touch=True)
    first = protected(e, TOUCH)
    assert (plan_orders(e, OrderRole.PROTECTIVE_STOP)[0].stop_price) == D("48.00")
    sync(e, mark="51.00")
    assert step(e, TOUCH, ev(e, "51.00")).action is A.PROTECTED

    # The target is touched: cancel the stop (potential gap), confirm, sell the bucket.
    sync(e, mark="54.00")
    r = step(e, TOUCH, ev(e, "54.00"))
    assert (r.action, r.client_order_id, r.protection) == (
        A.CANCEL_STOP_FOR_PARTIAL, first, P.POTENTIAL_GAP)  # fmt: skip
    sync(e, mark="54.00")
    assert gaps(e)[0].confirmed and e.order(first).status is S.CANCELED
    r = step(e, TOUCH, ev(e, "54.00"))
    assert r.action is A.PLACE_PARTIAL_EXIT and r.protection is P.UNPROTECTED
    (target,) = plan_orders(e, OrderRole.TARGET)
    assert (target.qty, target.limit_price) == (D(6), D("54.00"))
    assert e.sim.market_trade("MSFT", D("54.10")) == [target.client_order_id]
    sync(e, mark="54.10")

    # Breakeven: the whole partial bucket is sold, the runner is re-protected at 50.00.
    r = step(e, TOUCH, ev(e, "54.10"))
    assert r.action is A.PLACE_STOP and r.protection is P.PROTECTED and r.phase is ExitPhase.RUNNER
    runner = e.order(r.client_order_id or "")
    assert (runner.qty, runner.stop_price) == (D(6), D("50.00"))
    assert not gaps(e)[0].open and gaps(e)[0].closed_by_client_order_id == runner.client_order_id

    # Trailing: a new high is persisted with its evidence and the stop is raised.
    sync(e, mark="56.50")
    r = step(e, TOUCH, ev(e, "56.50", "57.00"))
    assert r.action is A.CANCEL_STOP_FOR_RAISE and "50.00 -> 55.50" in r.detail
    with e.db.read() as s:
        level = PlanStopLevelsRepository().latest(s, PLAN.plan_id)
    assert level is not None and level.source is StopLevelSource.TRAILING
    assert (level.stop_price, level.high_water, level.evidence["source"]) == (
        D("55.50"), D("57.00"), "SIM")  # fmt: skip
    sync(e, mark="56.50")
    r = step(e, TOUCH, ev(e, "56.50", "57.00"))
    assert r.action is A.PLACE_STOP and e.order(r.client_order_id or "").stop_price == D("55.50")
    assert step(e, TOUCH, ev(e, "56.00", "57.00")).action is A.PROTECTED  # a lower high

    # Trailing exit.
    assert e.sim.market_trade("MSFT", D("55.40")) == [r.client_order_id]
    sync(e, mark="55.40")
    done = step(e, TOUCH, ev(e, "55.40", "57.00"))
    assert (done.action, done.phase, done.protection) == (A.DONE, ExitPhase.CLOSED, P.FLAT)
    assert realized(e) == D("57.00")  # 6 x 4.10 + 6 x 5.40
    assert [g.confirmed for g in gaps(e)] == [True, True] and not any(g.open for g in gaps(e))
    for g in gaps(e):  # every unprotected interval is bounded and recorded
        assert g.closed_at is not None and g.confirmed_at is not None
        assert g.closed_at - g.opened_at <= timedelta(minutes=3)
    one_submit_per_order(e)
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_default_policy_defers_partial_and_never_sends_a_target(tmp_path: Path) -> None:
    e = env(tmp_path)
    stop = protected(e)
    sync(e, mark="54.50")
    r = step(e, evidence=ev(e, "54.50"))
    assert r.action is A.PARTIAL_DEFERRED and r.protection is P.PROTECTED
    assert e.order(stop).status is S.ACCEPTED and plan_orders(e, OrderRole.TARGET) == []
    with pytest.raises(ValueError, match="touch_partial_exit"):
        orch(e, TOUCH)  # the integrated OMS refuses the opt-in


# ---------------------------------------------------------- stop-outs, gaps


def test_stop_out_before_target(tmp_path: Path) -> None:
    e = env(tmp_path)
    stop = protected(e)
    assert e.sim.market_trade("MSFT", D("47.95")) == [stop]
    sync(e, mark="47.95")
    r = step(e, evidence=ev(e, "47.95"))
    assert (r.action, r.phase, r.protection) == (A.DONE, ExitPhase.CLOSED, P.FLAT)
    assert realized(e) == D("-24.60")
    assert gaps(e) == []  # never unprotected


def test_opening_gap_through_the_stop(tmp_path: Path) -> None:
    e = env(tmp_path)
    stop = protected(e)
    sync(e, MON_AFTER_CLOSE - e.now())
    r = step(e, evidence=ev(e, "44.00"))  # after hours: hold, keep the GTC stop
    assert r.action is A.HOLD_BLOCKED and "MARKET_CLOSED" in r.detail
    assert r.protection is P.PROTECTED and e.order(stop).status is S.ACCEPTED
    assert e.sim.market_trade("MSFT", D("44.00")) == []  # not an eligible print
    e.clock.advance_to(TUE_OPEN)
    assert e.sim.market_trade("MSFT", D("44.00")) == [stop]
    sync(e, mark="44.00")
    assert step(e).action is A.DONE
    assert e.order(stop).avg_fill_price == D("44.00") and realized(e) == D(-72)


def test_stop_partially_filled_still_covers_the_rest(tmp_path: Path) -> None:
    e = env(tmp_path)
    stop = protected(e)
    e.sim.market_trade("MSFT", D("47.90"), liquidity=D(5))
    sync(e, mark="47.90")
    r = step(e, evidence=ev(e, "47.90"))
    assert r.action is A.PROTECTED and r.protection is P.PROTECTED
    assert e.order(stop).status is S.PARTIALLY_FILLED


# --------------------------------------------------------- sizes and entries


def test_small_position_gets_full_protection_and_no_partial(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    stop = protected(e, TOUCH, qty="1")
    assert e.order(stop).qty == 1
    sync(e, mark="55.00")
    r = step(e, TOUCH, ev(e, "55.00"))
    assert (r.action, r.phase) == (A.PROTECTED, ExitPhase.SMALL_POSITION_UNRESOLVED)
    assert plan_orders(e, OrderRole.TARGET) == []


def test_partial_entry_fill_is_cancelled_confirmed_then_protected(tmp_path: Path) -> None:
    e = env(tmp_path, sim=SLOW_CANCEL)
    entry = enter(e, "12", fill="5")
    r = step(e)
    assert r.action is A.CANCEL_ENTRY_REMAINDER and r.protection is P.UNPROTECTED
    assert step(e).action is A.WAIT_CANCEL
    sync(e)
    assert e.order(entry).status is S.CANCELED
    r = step(e)
    assert r.action is A.PLACE_STOP and e.order(r.client_order_id or "").qty == 5


# --------------------------------------------- replacement and its failures


def raise_ready(e: OmsEnv, policy: ExitPolicy = DEFAULT) -> str:
    """A protected position with a durable higher level, ready for a raise."""
    stop = protected(e, policy)
    with e.db.transaction() as tx:
        PlanStopLevelsRepository().append(tx, PLAN.plan_id, stop_price=D("49.00"),
                                          high_water=None, source=StopLevelSource.BREAKEVEN,
                                          evidence={"reason": "raise"}, at=e.now())  # fmt: skip
    sync(e, mark="51.00")
    return stop


@pytest.mark.parametrize(
    "point",
    ["after_cancel_marker", "after_intent_commit", "send_marker_uncommitted",
     "after_send_marker", "after_submit", "after_outcome_commit"],
)  # fmt: skip
def test_restart_during_stop_replacement(tmp_path: Path, point: str) -> None:
    e = env(tmp_path)
    old = raise_ready(e)
    if point == "after_cancel_marker":
        e.rebuild(crash_at(point))
        with pytest.raises(BaseException, match=point):
            step(e, evidence=ev(e, "51.00"))
    else:
        assert step(e, evidence=ev(e, "51.00")).action is A.CANCEL_STOP_FOR_RAISE
        sync(e, mark="51.00")
        e.rebuild(crash_at(point))
        with pytest.raises(BaseException, match=point):
            step(e, evidence=ev(e, "51.00"))
    e.rebuild()  # restart
    e.advance(MIN)
    recover_after_restart(e.recovery, e.sync, now=e.now())
    sync(e, mark="51.00")
    old_status = e.order(old).status
    r = step(e, evidence=ev(e, "51.00"))
    one_submit_per_order(e)
    stops = plan_orders(e, OrderRole.PROTECTIVE_STOP)
    if point == "after_cancel_marker":  # the cancel never left: the old stop still works
        assert old_status is S.ACCEPTED and len(stops) == 1
        assert not gaps(e)[0].confirmed  # closed on the same stop, never confirmed
        assert r.action is A.CANCEL_STOP_FOR_RAISE  # the raise is simply retried
        return
    new = stops[-1]
    assert new.stop_price == D("49.00")  # the durable level, never lowered
    if point in ("after_submit", "after_outcome_commit"):  # the broker has it: adopted
        assert new.status is S.ACCEPTED and r.action is A.PROTECTED
        assert r.protection is P.PROTECTED and not gaps(e)[-1].open
    else:  # never sent: unresolved, never reported as protection
        assert new.status in (S.PENDING_SUBMIT, S.SUBMIT_AMBIGUOUS)
        assert (r.action, r.protection) == (A.WAIT_RESOLUTION, P.UNCERTAIN)
        assert gaps(e)[-1].open and gaps(e)[-1].confirmed
        assert not e.submits(new.client_order_id)


def test_declined_stop_cancel_keeps_protection(tmp_path: Path) -> None:
    e = env(tmp_path, sim=SLOW_CANCEL)
    old = raise_ready(e)
    e.sim.refuse_next_cancel(old)
    assert step(e, evidence=ev(e, "51.00")).action is A.CANCEL_STOP_FOR_RAISE
    assert step(e, evidence=ev(e, "51.00")).action is A.WAIT_CANCEL
    sync(e, mark="51.00")
    assert e.order(old).status is S.ACCEPTED
    (gap,) = gaps(e)
    assert not gap.confirmed and gap.closed_by_client_order_id == old
    assert step(e, evidence=ev(e, "51.00")).action is A.CANCEL_STOP_FOR_RAISE  # retried


def test_ambiguous_replacement_is_not_protection_until_adopted(tmp_path: Path) -> None:
    e = env(tmp_path)
    raise_ready(e)
    step(e, evidence=ev(e, "51.00"))
    sync(e, mark="51.00")
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    r = step(e, evidence=ev(e, "51.00"))
    assert r.action is A.PLACE_STOP and r.protection is P.UNCERTAIN
    assert step(e, evidence=ev(e, "51.00")).action is A.WAIT_RESOLUTION
    e.advance(MIN)
    recover_after_restart(e.recovery, e.sync, now=e.now())
    sync(e, mark="51.00")
    r = step(e, evidence=ev(e, "51.00"))
    assert (r.action, r.protection) == (A.PROTECTED, P.PROTECTED)


def test_failure_to_restore_protection_halts(tmp_path: Path) -> None:
    e = env(tmp_path)
    raise_ready(e)
    step(e, evidence=ev(e, "51.00"))
    sync(e, mark="51.00")
    e.sim.script_submit(Reject("rejected", 422))
    e.sim.script_submit(Reject("rejected again", 422))
    r = step(e, evidence=ev(e, "51.00"))
    assert r.action is A.PLACE_STOP and r.placement is not None
    assert r.placement.outcome is PlacementOutcome.REJECTED and r.protection is P.UNPROTECTED
    r = step(e, evidence=ev(e, "51.00"))
    assert r.action is A.HALT_PROTECTION_FAILED and r.protection is P.UNPROTECTED
    st = e.state()
    assert st.halted and st.reason_code == HaltReason.PROTECTION_FAILED.value
    assert gaps(e)[-1].open and gaps(e)[-1].confirmed  # the interval stays visible
    r = step(e, evidence=ev(e, "51.00"))
    assert r.action is A.HOLD_BLOCKED and "PERSISTENT_HALT" in r.detail


def test_partial_exit_timeout_restores_full_protection(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    protected(e, TOUCH)
    sync(e, mark="54.00")
    step(e, TOUCH, ev(e, "54.00"))
    sync(e, mark="54.00")
    assert step(e, TOUCH, ev(e, "54.00")).action is A.PLACE_PARTIAL_EXIT
    (target,) = plan_orders(e, OrderRole.TARGET)
    e.sim.market_trade("MSFT", D("54.00"), liquidity=D(2))  # only 2 of 6 sell
    sync(e, timedelta(minutes=2), mark="53.50")
    r = step(e, TOUCH, ev(e, "53.50"))
    assert r.action is A.CANCEL_PARTIAL_TIMEOUT and r.client_order_id == target.client_order_id
    sync(e, mark="53.50")
    r = step(e, TOUCH, ev(e, "54.20"))  # touched again, but the last order was the target
    assert r.action is A.PLACE_STOP and r.phase is ExitPhase.PROTECTED
    new = e.order(r.client_order_id or "")
    assert (new.qty, new.stop_price) == (D(10), D("48.00"))  # breakeven needs the whole bucket
    assert r.protection is P.PROTECTED


# --------------------------------------- blockers, evidence, breached stops


@pytest.mark.parametrize("how", ["halt", "reconciliation"])
def test_blockers_hold_everything_and_cancel_nothing(tmp_path: Path, how: str) -> None:
    e = env(tmp_path)
    stop = raise_ready(e)
    with e.db.transaction() as tx:
        if how == "halt":
            TradingStateRepository().halt(tx, HaltReason.OPERATOR, detail="t", actor="t",
                                          at=e.now())  # fmt: skip
        else:
            ReconciliationRunsRepository().append(tx, ReconciliationResult.MISMATCH, None,
                                                  e.now())  # fmt: skip
    r = step(e, evidence=ev(e, "51.00"))
    assert r.action is A.HOLD_BLOCKED and r.protection is P.PROTECTED
    assert e.order(stop).status is S.ACCEPTED and gaps(e) == []


def test_missing_or_stale_evidence_never_moves_a_stop(tmp_path: Path) -> None:
    e = env(tmp_path)
    raise_ready(e)
    for evidence, problem in [
        (None, "EVIDENCE_MISSING"),
        (ev(e, "51.00", age=timedelta(minutes=10)), "EVIDENCE_STALE"),
        (ev(e, "51.00", source="UNVERIFIED"), "EVIDENCE_SOURCE_NOT_ALLOWED"),
        (ev(e, "51.00", symbol="AAPL"), "EVIDENCE_SYMBOL_MISMATCH"),
        (ev(e, "51.00", "50.00"), "EVIDENCE_PRICES_INVALID"),
        (ev(e, "51.00", age=-MIN), "EVIDENCE_FROM_FUTURE"),
    ]:
        r = step(e, evidence=evidence)
        assert r.action is A.PROTECTED and problem in r.evidence_problems, (problem, r)
    assert len(plan_orders(e, OrderRole.PROTECTIVE_STOP)) == 1


def test_protection_does_not_wait_for_evidence(tmp_path: Path) -> None:
    e = env(tmp_path)
    enter(e)
    r = step(e, evidence=None)
    assert r.action is A.PLACE_STOP and r.evidence_problems == ("EVIDENCE_MISSING",)


def test_stop_level_already_breached_is_reported_not_acted_on(tmp_path: Path) -> None:
    e = env(tmp_path)
    enter(e)
    sync(e, mark="47.50")
    r = step(e, evidence=ev(e, "47.50"))
    assert (r.action, r.protection) == (A.STOP_BREACHED_UNPROTECTED, P.UNPROTECTED)
    assert plan_orders(e, OrderRole.PROTECTIVE_STOP) == []


def test_evidence_validation_is_strict() -> None:
    now = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    good = PriceEvidence("MSFT", D(50), D(51), now, "SIM", "sim", market_date(now), "obs-1")
    assert evidence_problems(good, symbol="MSFT", now=now, policy=ExitPolicy()) == ()
    naive = replace(good, as_of=now.replace(tzinfo=None))
    assert "EVIDENCE_TIMESTAMP_NAIVE" in evidence_problems(
        naive, symbol="MSFT", now=now, policy=ExitPolicy())  # fmt: skip
    wrong_day = replace(good, session=market_date(now) - timedelta(days=1))
    assert "EVIDENCE_SESSION_MISMATCH" in evidence_problems(
        wrong_day, symbol="MSFT", now=now, policy=ExitPolicy())  # fmt: skip
    with pytest.raises(ValueError, match="min_stop_raise"):
        ExitPolicy(min_stop_raise=D(0))
    with pytest.raises(ValueError, match="source and feed"):
        ExitPolicy(allowed_sources=frozenset())
    with pytest.raises(ValueError, match="source and feed"):
        ExitPolicy(allowed_feeds=frozenset())


def test_impostor_plan_is_refused(tmp_path: Path) -> None:
    e = env(tmp_path)
    enter(e)
    impostor = TradePlan(PLAN.plan_id, replace(INTENT, protective_stop=D("40.00")))
    r = orch(e).step(impostor, now=e.now())
    assert r.action is A.INVALID_PLAN and plan_orders(e, OrderRole.PROTECTIVE_STOP) == []


# ===================================================== Step 16.13A hardening
# ------------------------------------------- A. breached stop, no protection


def breach_events(e: OmsEnv) -> list[dict[str, object]]:
    (entry,) = plan_orders(e, OrderRole.ENTRY)
    with e.db.read() as s:
        events = OrderEventsRepository().list_for_order(s, entry.client_order_id)
    return [ev.detail for ev in events if ev.detail and ev.detail.get("event") == BREACHED]


def breach(e: OmsEnv, *, entered: bool = False) -> StepResult:
    if not entered:
        enter(e)
    sync(e, mark="47.50")
    return step(e, evidence=ev(e, "47.50"))


def test_breached_stop_raises_a_durable_halt_with_evidence_and_sends_nothing(
    tmp_path: Path,
) -> None:
    e = env(tmp_path)
    enter(e)
    submits_before = len(e.submits())
    r = breach(e, entered=True)
    assert (r.action, r.protection) == (A.STOP_BREACHED_UNPROTECTED, P.UNPROTECTED)
    assert "operator recovery required" in r.detail
    # No emergency order of any kind: no stop, no exit, no new submit.
    assert [o.order_role for o in plan_orders(e)] == [OrderRole.ENTRY]
    assert len(e.submits()) == submits_before
    # The persistent halt blocks every automated order, entries included.
    st = e.state()
    assert st.halted and st.reason_code == HaltReason.STOP_BREACHED_UNPROTECTED.value
    assert st.changed_by == ORCHESTRATOR_ACTOR
    status = e.r.env.gates.evaluate(now=e.now())
    assert status.persistent_halt and not status.new_entries_allowed
    assert status.protective_exit_blockers  # automated exits are held too (policy 5)
    # The unprotected-position evidence is preserved in the append-only audit trail.
    (detail,) = breach_events(e)
    evidence = detail["evidence"]
    assert isinstance(evidence, dict)
    assert (evidence["held_qty"], evidence["stop_level"], evidence["symbol"]) == (
        "12", "48.00", "MSFT")  # fmt: skip
    mark = evidence["mark"]
    assert isinstance(mark, dict) and mark["price"] == "47.50"
    assert evidence["last_protective_stop"] is None and evidence["trade_plan_id"] == PLAN.plan_id
    step_ev = evidence["step_evidence"]
    assert isinstance(step_ev, dict) and step_ev["last"] == "47.50"
    with e.db.read() as s:
        halts = TradingStateRepository().events(s)
    assert halts[-1].action == "HALT" and halts[-1].reason_code == st.reason_code


def test_breach_survives_restart_and_price_recovery(tmp_path: Path) -> None:
    e = env(tmp_path)
    breach(e)
    e.rebuild()  # restart
    e.advance(MIN)
    recover_after_restart(e.recovery, e.sync, now=e.now())
    for price in ("47.00", "52.00", "60.00"):  # the price recovers, then rallies
        sync(e, mark=price)
        r = step(e, evidence=ev(e, price))
        assert r.action is A.HOLD_BLOCKED and "PERSISTENT_HALT" in r.detail
        assert r.protection is P.UNPROTECTED
        st = e.state()
        assert st.halted and st.reason_code == HaltReason.STOP_BREACHED_UNPROTECTED.value
    assert [o.order_role for o in plan_orders(e)] == [OrderRole.ENTRY]
    with e.db.read() as s:
        assert [h.action for h in TradingStateRepository().events(s)][-1] == "HALT"
    assert len(breach_events(e)) == 1  # recorded once; holding does not re-record


def test_breach_clears_only_through_operator_resume(tmp_path: Path) -> None:
    e = env(tmp_path)
    breach(e)
    sync(e, mark="52.00")
    # Without a clean reconciliation taken after the halt, the resume is refused.
    with e.db.transaction() as tx:
        ReconciliationRunsRepository().append(tx, ReconciliationResult.MISMATCH, None, e.now())
    assert not resume_after_clean_reconciliation(
        e.db, actor="operator", detail="t", now=e.now(), max_age=timedelta(minutes=10))  # fmt: skip
    assert e.state().halted
    sync(e, mark="52.00")  # a fresh clean reconciliation run
    assert resume_after_clean_reconciliation(
        e.db, actor="operator", detail="re-protect", now=e.now(),
        max_age=timedelta(minutes=10))  # fmt: skip
    r = step(e, evidence=ev(e, "52.00"))
    assert (r.action, r.protection) == (A.PLACE_STOP, P.PROTECTED)
    assert e.order(r.client_order_id or "").stop_price == D("48.00")


def test_breach_still_present_after_resume_halts_again(tmp_path: Path) -> None:
    e = env(tmp_path)
    breach(e)
    sync(e, mark="47.00")
    assert resume_after_clean_reconciliation(
        e.db, actor="operator", detail="t", now=e.now(), max_age=timedelta(minutes=10))  # fmt: skip
    r = step(e, evidence=ev(e, "47.00"))
    assert r.action is A.STOP_BREACHED_UNPROTECTED
    assert e.state().reason_code == HaltReason.STOP_BREACHED_UNPROTECTED.value
    assert len(breach_events(e)) == 2 and plan_orders(e, OrderRole.PROTECTIVE_STOP) == []


def test_breach_after_a_stop_ended_keeps_the_gap_evidence(tmp_path: Path) -> None:
    e = env(tmp_path)
    old = raise_ready(e)
    assert step(e, evidence=ev(e, "51.00")).action is A.CANCEL_STOP_FOR_RAISE
    sync(e, mark="48.50")  # cancel confirmed; the price falls through the 49.00 level
    r = step(e, evidence=ev(e, "48.50"))
    assert r.action is A.STOP_BREACHED_UNPROTECTED
    (gap,) = gaps(e)
    assert gap.open and gap.confirmed  # the unprotected interval stays open and visible
    evidence = breach_events(e)[0]["evidence"]
    assert isinstance(evidence, dict) and evidence["stop_level"] == "49.00"
    last_stop, open_gap = evidence["last_protective_stop"], evidence["open_gap"]
    assert isinstance(last_stop, dict) and last_stop["client_order_id"] == old
    assert last_stop["status"] == S.CANCELED.value
    assert isinstance(open_gap, dict) and open_gap["id"] == gap.id and open_gap["confirmed"]


def test_protection_failed_halt_records_its_evidence(tmp_path: Path) -> None:
    e = env(tmp_path)
    raise_ready(e)
    step(e, evidence=ev(e, "51.00"))
    sync(e, mark="51.00")
    e.sim.script_submit(Reject("rejected", 422))
    e.sim.script_submit(Reject("rejected again", 422))
    step(e, evidence=ev(e, "51.00"))
    assert step(e, evidence=ev(e, "51.00")).action is A.HALT_PROTECTION_FAILED
    (entry,) = plan_orders(e, OrderRole.ENTRY)
    with e.db.read() as s:
        events = OrderEventsRepository().list_for_order(s, entry.client_order_id)
    (failed,) = [x.detail for x in events if x.detail and x.detail["event"] == "PROTECTION_FAILED"]
    rejected = failed["rejected_stops"]
    assert isinstance(rejected, list) and len(rejected) == 2


# ---------------------------------------------- B. trailing evidence contract


def runner(e: OmsEnv) -> None:
    """A runner protected at breakeven (simulation-only opt-in path)."""
    protected(e, TOUCH)
    sync(e, mark="54.00")
    step(e, TOUCH, ev(e, "54.00"))
    sync(e, mark="54.00")
    assert step(e, TOUCH, ev(e, "54.00")).action is A.PLACE_PARTIAL_EXIT
    (target,) = plan_orders(e, OrderRole.TARGET)
    e.sim.market_trade("MSFT", D("54.10"))
    sync(e, mark="54.10")
    r = step(e, TOUCH, ev(e, "54.10"))
    assert r.action is A.PLACE_STOP and r.phase is ExitPhase.RUNNER and target.qty == 6


def levels(e: OmsEnv) -> list[tuple[Decimal, Decimal | None]]:
    with e.db.read() as s:
        return [(x.stop_price, x.high_water)
                for x in PlanStopLevelsRepository().list_for_plan(s, PLAN.plan_id)]  # fmt: skip


def test_duplicate_observation_is_applied_once(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    runner(e)
    sync(e, mark="55.00")
    first = ev(e, "51.00", "51.50")
    r = step(e, TOUCH, first)
    assert r.action is A.RECORD_HIGH and levels(e)[-1] == (D("50.00"), D("51.50"))
    before = levels(e)
    again = step(e, TOUCH, first)  # re-delivered: same observation id
    assert again.action is A.PROTECTED and "EVIDENCE_DUPLICATE" in again.evidence_problems
    # Same id, even with a fresher timestamp and a higher price: still the same observation.
    sync(e, mark="55.00")
    forged = replace(first, as_of=e.now(), session_high=D("59.00"))
    r = step(e, TOUCH, forged)
    assert r.action is A.PROTECTED and "EVIDENCE_DUPLICATE" in r.evidence_problems
    assert levels(e) == before


def test_older_observation_is_never_applied(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    runner(e)
    sync(e, mark="55.00")
    assert step(e, TOUCH, ev(e, "51.00", "51.50")).action is A.RECORD_HIGH
    before = levels(e)
    late = ev(e, "51.00", "58.00", age=timedelta(seconds=30), observation_id="late-1")
    r = step(e, TOUCH, late)  # fresh enough, but older than the applied observation
    assert r.action is A.PROTECTED and r.evidence_problems == ("EVIDENCE_OUT_OF_ORDER",)
    same_time = ev(e, "51.00", "58.00", observation_id="same-time-2")
    assert "EVIDENCE_OUT_OF_ORDER" in step(e, TOUCH, same_time).evidence_problems
    assert levels(e) == before


def test_persistence_boundary_rejects_replayed_observations(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    runner(e)
    sync(e, mark="55.00")
    obs = ev(e, "51.00", "51.50")
    step(e, TOUCH, obs)
    with e.db.transaction() as tx:  # a duplicate id is a no-op
        assert record_high_observation(tx, PLAN, high=D("60.00"), evidence=obs.as_dict(),
                                       at=e.now()) is None  # fmt: skip
    older = replace(obs, observation_id="older", as_of=obs.as_of - SEC5).as_dict()
    with pytest.raises(ValueError, match="not newer"), e.db.transaction() as tx:
        record_high_observation(tx, PLAN, high=D("60.00"), evidence=older, at=e.now())
    assert levels(e)[-1] == (D("50.00"), D("51.50"))


def test_evidence_outside_the_permitted_session_is_refused(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    runner(e)
    sync(e, mark="55.00")
    lenient = replace(TOUCH, max_evidence_age=timedelta(days=7))
    with e.db.read() as s:
        session = CalendarStore().load(s).sessions[market_date(e.now())]
    pre = session.open_at - MIN
    pre_market = ev(e, "55.00", "58.00", age=e.now() - pre, observation_id="pre-1")
    saturday = market_date(e.now()) - timedelta(days=(market_date(e.now()).weekday() + 2) % 7)
    sat_at = datetime.combine(saturday, datetime.min.time(), UTC) + timedelta(hours=16)
    weekend = ev(e, "55.00", "58.00", age=e.now() - sat_at, session=saturday,
                 observation_id="sat-1")  # fmt: skip
    for evidence, problem in [
        (pre_market, "EVIDENCE_OUTSIDE_SESSION"),
        (weekend, "EVIDENCE_SESSION_UNKNOWN"),
    ]:
        r = orch(e, lenient).step(PLAN, now=e.now(), evidence=evidence)
        assert r.action is A.PROTECTED and problem in r.evidence_problems, (problem, r)
    assert levels(e)[-1][1] is None  # no high was ever recorded


NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
GOOD = PriceEvidence("MSFT", D(50), D(51), NOW, "SIM", "sim", market_date(NOW), "obs-1")


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"symbol": "AAPL"}, "EVIDENCE_SYMBOL_MISMATCH"),
        ({"as_of": NOW + SEC5}, "EVIDENCE_FROM_FUTURE"),
        ({"as_of": NOW - timedelta(minutes=3)}, "EVIDENCE_STALE"),
        ({"as_of": NOW.replace(tzinfo=None)}, "EVIDENCE_TIMESTAMP_NAIVE"),
        ({"session": market_date(NOW) - timedelta(days=1)}, "EVIDENCE_SESSION_MISMATCH"),
        ({"session": NOW}, "EVIDENCE_SESSION_MISMATCH"),
        ({"feed": "iex"}, "EVIDENCE_FEED_NOT_ALLOWED"),
        ({"feed": ""}, "EVIDENCE_FEED_NOT_ALLOWED"),
        ({"source": "UNVERIFIED"}, "EVIDENCE_SOURCE_NOT_ALLOWED"),
        ({"last": D("NaN")}, "EVIDENCE_PRICES_INVALID"),
        ({"session_high": D("Infinity")}, "EVIDENCE_PRICES_INVALID"),
        ({"last": D("-1")}, "EVIDENCE_PRICES_INVALID"),
        ({"last": D("50.000001")}, "EVIDENCE_PRICES_INVALID"),
        ({"last": 50.0}, "EVIDENCE_PRICES_INVALID"),
        ({"session_high": D("49.99")}, "EVIDENCE_PRICES_INVALID"),
        ({"observation_id": ""}, "EVIDENCE_IDENTITY_INVALID"),
        ({"observation_id": "has space"}, "EVIDENCE_IDENTITY_INVALID"),
        ({"observation_id": "x" * 129}, "EVIDENCE_IDENTITY_INVALID"),
        ({"observation_id": None}, "EVIDENCE_IDENTITY_INVALID"),
    ],
)
def test_evidence_contract_rejects(changes: dict[str, object], problem: str) -> None:
    bad = replace(GOOD, **changes)  # type: ignore[arg-type]
    assert evidence_problems(GOOD, symbol="MSFT", now=NOW, policy=DEFAULT) == ()
    assert problem in evidence_problems(bad, symbol="MSFT", now=NOW, policy=DEFAULT)


def test_trailing_levels_only_rise_and_survive_restart(tmp_path: Path) -> None:
    e = env(tmp_path, touch=True)
    runner(e)
    for high in ("55.80", "55.20", "56.10", "55.90"):  # rises, dips, rises, dips
        sync(e, mark="55.00")
        r = step(e, TOUCH, ev(e, "55.00", high))
        if r.action is A.CANCEL_STOP_FOR_RAISE:  # re-protect at the durable level
            sync(e, mark="55.00")
            assert step(e, TOUCH, ev(e, "55.00", high)).action is A.PLACE_STOP
    history = levels(e)
    assert [s for s, _ in history] == sorted(s for s, _ in history)
    highs = [h for _, h in history if h is not None]
    assert highs == sorted(highs) and highs[-1] == D("56.10")
    with e.db.read() as s:
        _, before = protected_state(s, PLAN)
    e.rebuild()  # restart: the level is reconstructed from durable state alone
    with e.db.read() as s:
        _, after = protected_state(s, PLAN)
    assert before == after == D("54.60")
    stop = plan_orders(e, OrderRole.PROTECTIVE_STOP)[-1]
    assert stop.stop_price is not None and stop.stop_price <= D("54.60")


# ------------------------------------------ C. simulation-only partial profits


def test_application_configuration_cannot_enable_partial_targets(tmp_path: Path) -> None:
    app = AppConfig.load(CONFIG_DIR)
    for mode in RuntimeMode:
        assert app.oms(mode).allow_unprotected_targets is False
    e = oms_env(tmp_path, config=app.oms(RuntimeMode.SIMULATE))
    with pytest.raises(ValueError, match="touch_partial_exit"):
        ExitOrchestrator(e.db, e.om, e.r.env.gates, TOUCH)
    assert ExitPolicy().touch_partial_exit is False  # PARTIAL_DEFERRED by default


@pytest.mark.parametrize(
    "key", ["allow_unprotected_targets", "touch_partial_exit", "partial_profit"]
)
@pytest.mark.parametrize("name", ["risk.yaml", "operating_policy.yaml"])
def test_config_files_refuse_partial_target_switches(tmp_path: Path, name: str, key: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    (tmp_path / name).write_text(f"{text}\n{key}: true\n", encoding="utf-8")
    loader = load_risk_limits if name == "risk.yaml" else load_operating_policy
    with pytest.raises(ConfigFileError, match=key):
        loader(tmp_path / name)


def test_no_application_code_sets_the_opt_ins() -> None:
    """Structural guarantee: only tests set the simulation-only switches."""
    src = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
    switch = re.compile(r"\b(allow_unprotected_targets|touch_partial_exit)\s*=(?!=)")
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py") if switch.search(p.read_text("utf-8"))
    ]
    assert offenders == []
