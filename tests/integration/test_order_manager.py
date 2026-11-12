"""M1 step 12: order lifecycle, ambiguity, recovery and accounting consistency."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import OrderNotFoundError
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import (
    Accept,
    AcceptThenFail,
    AuthFailure,
    FailBeforeReceipt,
    RateLimit,
    Reject,
    SimConfig,
)
from swing_trading_bot.domain.enums import (
    HaltReason,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.portfolio import FillOutcome
from swing_trading_bot.ledger.repositories import (
    ReservationStatus,
    ResolutionAttemptsRepository,
    TradingStateRepository,
)
from swing_trading_bot.oms.execution import SimulationExecution
from swing_trading_bot.oms.order_manager import (
    CancelOutcome,
    ExecutionNotPermittedError,
    OrderManager,
    PlacementOutcome,
    RefusalCode,
)
from swing_trading_bot.oms.reconciler import Reconciler
from swing_trading_bot.oms.recovery import AttemptResult
from swing_trading_bot.oms.sync import SyncStatus
from tests.support.alpaca_fake import FakeTransport
from tests.support.oms import OMS_CONFIG, OmsEnv, oms_env
from tests.support.portfolio import NOW

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)


@pytest.fixture
def e(tmp_path: Path) -> OmsEnv:
    return oms_env(tmp_path)


def fill(e: OmsEnv, coid: str, qty: str, price: str = "50.00", **kw: object) -> None:
    e.sim.schedule_fill(coid, D(qty), D(price), after=timedelta(seconds=5), **kw)  # type: ignore[arg-type]


def sync(e: OmsEnv, after: timedelta = MIN) -> SyncStatus:
    e.advance(after)
    return e.sync.sync(now=e.now()).status


# ------------------------------------------------------------- acknowledgments


def test_successful_submission_and_acknowledgment(e: OmsEnv) -> None:
    r = e.place()
    assert r.outcome is PlacementOutcome.ACCEPTED and r.client_order_id is not None
    coid = r.client_order_id
    order = e.order(coid)
    assert order.status is S.ACCEPTED and order.broker_order_id is not None
    assert order.send_attempted_at == NOW and order.qty == D(12)
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert e.reservation(coid).remaining == D(600)
    assert e.events(coid) == ["INTENT_CREATED", "SEND_ATTEMPT", "ADOPTED", "BROKER_STATUS"]
    assert len(e.submits(coid)) == 1
    assert e.balances().available_cash == D(4400)


def test_definitive_rejection_releases_the_reservation(e: OmsEnv) -> None:
    e.sim.script_submit(Reject("insufficient qty", 403))
    r = e.place()
    assert r.outcome is PlacementOutcome.REJECTED and r.status is S.REJECTED
    assert r.client_order_id is not None
    assert e.reservation(r.client_order_id).status is ReservationStatus.RELEASED
    assert e.balances().available_cash == D(5000)
    assert e.order(r.client_order_id).reject_reason is not None


def test_partial_then_complete_fill(e: OmsEnv) -> None:
    coid = e.place().client_order_id
    assert coid is not None
    fill(e, coid, "5")
    assert sync(e) is SyncStatus.OK
    assert e.order(coid).status is S.PARTIALLY_FILLED and e.order(coid).filled_qty == D(5)
    assert e.reservation(coid).remaining == D(350)  # shrinks per filled share
    fill(e, coid, "7", "49.90")
    assert sync(e) is SyncStatus.OK
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.FILLED, D(12))
    assert e.reservation(coid).status is ReservationStatus.CONSUMED
    b = e.balances()
    assert b.cash == D(5000) - D(250) - D("349.30")
    assert b.buy_commitments == 0 and b.cost_basis_exposure == D("599.30")
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_partial_then_cancel_keeps_reservation_until_confirmed(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SimConfig(cancel_delay=timedelta(seconds=30)))
    coid = e.place().client_order_id
    assert coid is not None
    fill(e, coid, "5")
    sync(e)
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.REQUESTED
    assert e.order(coid).status is S.PENDING_CANCEL
    res = e.reservation(coid)
    assert res.status is ReservationStatus.ACTIVE and res.remaining == D(350)  # not released
    e.advance(timedelta(seconds=10))
    e.sync.sync(now=e.now())  # broker still pending_cancel
    assert e.order(coid).status is S.PENDING_CANCEL
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    sync(e)  # cancel confirmed by the broker
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.CANCELED, D(5))
    assert e.reservation(coid).status is ReservationStatus.RELEASED
    assert e.balances().available_cash == D(5000) - D(250)
    assert "CANCEL_REQUESTED" in e.events(coid) and "CANCEL_SENT" in e.events(coid)


def test_cancel_racing_a_fill(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SimConfig(cancel_delay=timedelta(seconds=30)))
    coid = e.place().client_order_id
    assert coid is not None
    # The fill executes 10s from now but its activity posts 3 minutes late.
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=10),
                        activity_delay=timedelta(minutes=3))  # fmt: skip
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.REQUESTED
    e.advance(MIN)
    report = e.sync.sync(now=e.now())
    # broker: filled (the cancel lost the race); local: fill not yet booked
    assert coid in report.awaiting_fills
    assert e.order(coid).status is S.PENDING_CANCEL
    assert e.reservation(coid).status is ReservationStatus.ACTIVE  # never released early
    sync(e, timedelta(minutes=3))
    assert e.order(coid).status is S.FILLED and e.order(coid).filled_qty == D(12)
    assert e.reservation(coid).status is ReservationStatus.CONSUMED
    assert e.r.env.pf.check_invariants(now=e.now()).ok


def test_expiration_and_post_acceptance_rejection_release(e: OmsEnv) -> None:
    a = e.place().client_order_id
    assert a is not None
    e.sim.schedule_expire(a, after=timedelta(seconds=5))
    sync(e)
    assert e.order(a).status is S.EXPIRED
    assert e.reservation(a).status is ReservationStatus.RELEASED
    e.r.set_marks({"AAPL": "100.00"}, at=e.now())
    b = e.place("AAPL", "100.00", "99.00").client_order_id
    assert b is not None
    e.sim.schedule_reject(b, after=timedelta(seconds=5))
    sync(e)
    assert e.order(b).status is S.REJECTED
    assert e.reservation(b).status is ReservationStatus.RELEASED


def test_cancel_refused_for_ambiguous_and_terminal_orders(e: OmsEnv) -> None:
    e.sim.script_submit(FailBeforeReceipt())
    coid = e.place().client_order_id
    assert coid is not None
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.NOT_CANCELABLE_LOCALLY
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS


# ------------------------------------------------------------- duplicates


def test_duplicate_client_order_id_at_the_broker_is_adopted(tmp_path: Path) -> None:
    coid = new_client_order_id()
    e = oms_env(tmp_path)
    e.rebuild()
    e.om._new_id = lambda: coid
    e.sim.submit_order(SubmitOrderRequest(coid, "MSFT", Side.BUY, D(12), OrderType.LIMIT,
                                          TimeInForce.DAY, D("50.00")))  # fmt: skip
    r = e.place()
    assert r.outcome is PlacementOutcome.ACCEPTED and r.status is S.ACCEPTED
    assert "duplicate" in r.detail
    assert len(e.submits(coid)) == 2 and "duplicate" in e.submits(coid)[-1]
    assert len(e.sim.get_open_orders()) == 1  # still one broker order
    assert e.reservation(coid).status is ReservationStatus.ACTIVE  # never treated as reject


def test_local_client_order_id_collision_sends_nothing(e: OmsEnv) -> None:
    first = e.place().client_order_id
    assert first is not None
    e.om._new_id = lambda: first
    e.r.set_marks({"AAPL": "100.00"}, at=e.now())
    with pytest.raises(PersistenceError):
        e.place("AAPL", "100.00", "99.00")
    assert len(e.submits()) == 1


def test_same_intent_cannot_be_submitted_twice(e: OmsEnv) -> None:
    decision = e.decide()
    assert e.om.place_entry(decision, now=e.now()).outcome is PlacementOutcome.ACCEPTED
    again = e.om.place_entry(decision, now=e.now())
    assert again.refusal is RefusalCode.LEDGER_CHANGED
    fresh = e.decide()  # same intent key, re-evaluated
    assert {"DUPLICATE_INTENT", "DUPLICATE_WORKING_ORDER"} <= set(fresh.rejection_codes)
    assert len(e.submits()) == 1


def test_duplicate_and_conflicting_fill_ids(e: OmsEnv) -> None:
    coid = e.place().client_order_id
    assert coid is not None
    fill(e, coid, "12")
    sync(e)
    report = e.sync.sync(now=e.now())  # overlap re-read of the same activity
    assert set(report.fills.values()) == {FillOutcome.DUPLICATE}
    assert e.order(coid).filled_qty == D(12)

    def tamper(raw: dict[str, object]) -> dict[str, object]:
        return {**raw, "qty": "11"}

    e.sim.corrupt_next("get_activities", tamper)
    conflict = e.sync.sync(now=e.now())
    assert FillOutcome.CONFLICT in conflict.fills.values()
    assert conflict.status is SyncStatus.HALTED
    assert e.state().reason_code == HaltReason.RECON_EXECUTION
    assert e.order(coid).filled_qty == D(12)  # books untouched


def test_out_of_order_fill_is_booked_then_halts(e: OmsEnv) -> None:
    coid = e.place().client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(5), D(50), after=timedelta(seconds=5),
                        activity_delay=timedelta(minutes=20))  # fmt: skip
    e.sim.schedule_fill(coid, D(7), D(50), after=timedelta(seconds=10))
    sync(e)
    assert e.order(coid).filled_qty == D(7)
    sync(e, timedelta(minutes=25))
    assert e.order(coid).filled_qty == D(12)  # recorded first ...
    assert e.state().reason_code in {HaltReason.RECON_EXECUTION, HaltReason.LEDGER_INVARIANT}
    with e.db.read() as s:
        reasons = {h.reason_code for h in TradingStateRepository().events(s)}
    assert HaltReason.RECON_EXECUTION in reasons  # ... then halts (out of order)


def test_unknown_and_non_bot_fills_are_preserved_and_halt(e: OmsEnv) -> None:
    stray = new_client_order_id()  # bot-shaped id the ledger never created
    e.sim.submit_order(SubmitOrderRequest(stray, "MSFT", Side.BUY, D(2), OrderType.LIMIT,
                                          TimeInForce.DAY, D("50.00")))  # fmt: skip
    e.sim.schedule_fill(stray, D(2), D(50), after=timedelta(seconds=5))
    report = e.sync.sync(now=e.advance(MIN))
    assert FillOutcome.ORPHAN in report.fills.values()
    assert e.order(stray).status is S.ORPHAN
    assert e.state().halted and e.state().reason_code in {"RECON_EXECUTION", "LEDGER_INVARIANT"}
    manual = "manual-order-1"
    e.sim.submit_order(SubmitOrderRequest(manual, "MSFT", Side.BUY, D(1), OrderType.LIMIT,
                                          TimeInForce.DAY, D("50.00")))  # fmt: skip
    e.sim.schedule_fill(manual, D(1), D(50), after=timedelta(seconds=5))
    report = e.sync.sync(now=e.advance(MIN))
    assert FillOutcome.UNATTRIBUTED in report.fills.values()
    with e.db.read() as s:
        n = s.fetch_one("SELECT count(*) FROM broker_activities WHERE attribution = 'UNEXPLAINED'")
    assert n is not None and n[0] == 1  # preserved


# ----------------------------------------------------------- ambiguous sends


@pytest.mark.parametrize("failure", ["gateway_timeout", "read_timeout"])
def test_accepted_then_transport_failure_is_adopted(e: OmsEnv, failure: str) -> None:
    e.sim.script_submit(AcceptThenFail(failure))  # type: ignore[arg-type]
    r = e.place()
    assert r.outcome is PlacementOutcome.AMBIGUOUS and r.client_order_id is not None
    coid = r.client_order_id
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert e.r.gate(e.now()).new_entries_allowed is False  # blocks new entries
    fill(e, coid, "12")
    e.advance(MIN)
    e.recovery.cycle(now=e.now())
    # adopted; the broker reports it filled but no fill is booked yet: stays working
    assert e.order(coid).status is S.ACCEPTED
    e.sync.sync(now=e.now())
    assert e.order(coid).status is S.FILLED
    assert len(e.submits(coid)) == 1 and len(e.sim.get_orders(after=NOW - MIN)) == 1


def test_rate_limited_or_auth_failed_submit_is_ambiguous(e: OmsEnv) -> None:
    e.sim.script_submit(RateLimit())
    r = e.place()
    assert r.outcome is PlacementOutcome.AMBIGUOUS and "429" in r.detail
    e2_coid = r.client_order_id
    assert e2_coid is not None and e.reservation(e2_coid).status is ReservationStatus.ACTIVE


def test_auth_failure_on_submit_halts(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    e.sim.script_submit(AuthFailure())
    r = e.place()
    assert r.outcome is PlacementOutcome.AMBIGUOUS
    assert e.state().reason_code == HaltReason.AUTH


def reconcile(e: OmsEnv) -> None:
    """A real reconciliation run (one second later) against an INIT baseline."""
    from tests.support.recon import RECON_CONFIG, baseline  # noqa: PLC0415

    with e.db.read() as s:
        has_baseline = s.fetch_one("SELECT 1 FROM broker_baselines") is not None
    if not has_baseline:
        baseline(e)
    e.clock.advance(timedelta(seconds=1))
    env = e.r.env
    rep = Reconciler(env.db, env.pf, env.sim, RECON_CONFIG).run(now=e.now())
    assert rep.codes() == {"AMBIGUOUS_ORDER_UNRESOLVED"}, rep.findings


def _lost(e: OmsEnv) -> str:
    e.sim.script_submit(FailBeforeReceipt())
    r = e.place()
    assert r.outcome is PlacementOutcome.AMBIGUOUS and r.client_order_id is not None
    return r.client_order_id


def test_order_lost_before_broker_receipt_needs_full_evidence_and_operator(e: OmsEnv) -> None:
    coid = _lost(e)
    for minutes in (1, 4):
        e.advance(timedelta(minutes=minutes))
        e.recovery.cycle(now=e.now())
    refused = e.recovery.confirm_absent(coid, operator="alice", now=e.now())
    assert not refused.complete and e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    e.advance(timedelta(minutes=21))  # 26 minutes after the send
    e.recovery.cycle(now=e.now())
    assert e.recovery.list_scan(coid, now=e.now()) is AttemptResult.NOT_FOUND
    assert e.recovery.activity_scan(coid, now=e.now()) is AttemptResult.CLEAN
    still = e.recovery.confirm_absent(coid, operator="alice", now=e.now())
    assert still.missing == ("no clean, fresh reconciliation run after the evidence was gathered",)
    reconcile(e)
    done = e.recovery.confirm_absent(coid, operator="alice", now=e.now() + timedelta(seconds=2))
    assert done.complete and done.not_found_lookups == 3
    assert e.order(coid).status is S.CONFIRMED_ABSENT
    assert e.reservation(coid).status is ReservationStatus.RELEASED
    with e.db.read() as s:
        results = [a.result for a in ResolutionAttemptsRepository().list_for_order(s, coid)]
    assert results.count("REFUSED") == 2 and results[-1] == "CONFIRMED_ABSENT"
    assert len(e.submits(coid)) == 1  # never resent


def test_confirmed_absent_order_that_appears_is_adopted_and_halts(e: OmsEnv) -> None:
    coid = _lost(e)
    for minutes in (1, 4, 21):
        e.advance(timedelta(minutes=minutes))
        e.recovery.cycle(now=e.now())
    e.recovery.list_scan(coid, now=e.now())
    e.recovery.activity_scan(coid, now=e.now())
    reconcile(e)
    assert e.recovery.confirm_absent(
        coid, operator="op", now=e.now() + timedelta(seconds=2)
    ).complete
    # It turns up after all (e.g. a broker-side replay of the lost request).
    e.sim.submit_order(SubmitOrderRequest(coid, "MSFT", Side.BUY, D(12), OrderType.LIMIT,
                                          TimeInForce.DAY, D("50.00")))  # fmt: skip
    e.recovery.lookup(coid, now=e.advance(MIN))
    assert e.order(coid).status is S.ACCEPTED
    assert e.state().reason_code == HaltReason.RECON_ORDER


def test_delayed_visibility_is_not_released_early(e: OmsEnv) -> None:
    e.sim.script_submit(AcceptThenFail("gateway_timeout", visibility_delay=timedelta(minutes=8)))
    coid = e.place().client_order_id
    assert coid is not None
    for _ in range(7):  # not found for 7 minutes
        e.advance(MIN)
        e.recovery.cycle(now=e.now())
        assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert not e.recovery.confirm_absent(coid, operator="op", now=e.now()).complete
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    e.advance(timedelta(minutes=2))
    e.recovery.cycle(now=e.now())
    assert e.order(coid).status is S.ACCEPTED
    assert len(e.submits(coid)) == 1


def test_ambiguous_for_ten_then_thirty_minutes(e: OmsEnv) -> None:
    coid = _lost(e)
    e.advance(timedelta(minutes=10))
    assert e.recovery.cycle(now=e.now()).escalated == []
    assert not e.state().halted and e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    e.advance(timedelta(minutes=20, seconds=1))
    assert e.recovery.cycle(now=e.now()).escalated == [coid]
    assert e.state().reason_code == HaltReason.AMBIGUOUS_ORDER_TIMEOUT
    assert e.recovery.cycle(now=e.advance(MIN)).escalated == []  # one halt per order
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS  # still not released by a timer
    assert e.reservation(coid).status is ReservationStatus.ACTIVE


def test_repeated_404_lookups_never_mean_rejection(e: OmsEnv) -> None:
    coid = _lost(e)
    for _ in range(3):
        e.sim.fail_next("get_order_by_client_id", OrderNotFoundError("404", status_code=404))
        e.advance(timedelta(minutes=5))
        assert e.recovery.lookup(coid, now=e.now()) is S.SUBMIT_AMBIGUOUS
    with e.db.read() as s:
        attempts = ResolutionAttemptsRepository().list_for_order(s, coid)
    assert [a.result for a in attempts] == ["NOT_FOUND"] * 3
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert len(e.submits(coid)) == 1


# ------------------------------------------------------ stale authorization


def test_stale_decision_and_changed_fingerprint_are_refused(e: OmsEnv) -> None:
    d = e.decide()
    e.r.set_marks({"MSFT": "50.01"}, at=e.now())  # evidence changed
    r = e.om.place_entry(d, now=e.now())
    assert r.refusal is RefusalCode.LEDGER_CHANGED and r.client_order_id is None
    late = e.decide()
    expired = e.om.place_entry(late, now=e.now() + timedelta(seconds=31))
    assert expired.refusal is RefusalCode.DECISION_EXPIRED
    rejected = e.decide(qty="999")
    assert e.om.place_entry(rejected, now=e.now()).refusal is RefusalCode.NOT_APPROVED
    assert e.submits() == []


def test_revalidation_inside_the_transaction(e: OmsEnv) -> None:
    """Ledger unchanged (same fingerprint) but the fresh broker evidence read at placement
    no longer passes: the in-transaction re-evaluation refuses and nothing is written."""
    d = e.decide()
    before = e.r.engine.fingerprint()
    e.sim.corrupt_next("get_account", lambda raw: {**raw, "trading_blocked": True})
    r = e.om.place_entry(d, now=e.now())
    assert r.refusal is RefusalCode.REVALIDATION_FAILED and "ACCOUNT_RESTRICTED" in r.detail
    assert e.r.engine.fingerprint() == before and e.submits() == []


def test_competing_intents_cannot_reserve_the_same_funds(e: OmsEnv) -> None:
    e.r.set_marks({"AAPL": "50.00"}, at=e.now())
    a = e.decide("MSFT", qty="12")
    b = e.decide("AAPL", qty="12")
    assert a.approved and b.approved
    assert a.evidence.ledger_fingerprint == b.evidence.ledger_fingerprint
    assert e.om.place_entry(a, now=e.now()).outcome is PlacementOutcome.ACCEPTED
    lost = e.om.place_entry(b, now=e.now())
    assert lost.refusal is RefusalCode.LEDGER_CHANGED  # b's evidence no longer holds
    assert e.balances().buy_commitments == D(600)


def test_competing_intents_when_cash_is_short(tmp_path: Path) -> None:
    from tests.support.pretrade import RiskEnv  # noqa: PLC0415

    e = oms_env(tmp_path)
    r: RiskEnv = e.r
    # leave $900 of settled cash: buy and sell the same day (proceeds unsettled)
    from tests.support.loss_limits import hold  # noqa: PLC0415
    from tests.support.portfolio import fill as pf_fill  # noqa: PLC0415
    from tests.support.portfolio import finish, make_order  # noqa: PLC0415

    hold(e.db, r.env.pf, "ZZZ", "41", "100.00", at=NOW)
    sale = make_order(e.db, side=Side.SELL, symbol="ZZZ", qty="41", limit="100.00")
    r.env.pf.process_fill(pf_fill(sale, "41", "100.00", at=NOW), now=NOW)
    finish(r.env.pf, e.db, sale, S.FILLED)
    r.set_marks({"MSFT": "50.00", "AAPL": "50.00"}, at=NOW)
    a, b = e.decide("MSFT"), e.decide("AAPL")
    assert a.approved_qty == b.approved_qty == D(12)  # each fits alone ($600 of $900)
    assert e.om.place_entry(a, now=e.now()).outcome is PlacementOutcome.ACCEPTED
    assert e.om.place_entry(b, now=e.now()).refusal is RefusalCode.LEDGER_CHANGED
    again = e.decide("AAPL")
    assert again.approved_qty == D(6)  # only $300 left after A's reservation
    assert e.balances().available_cash == D(300)


# ------------------------------------------------------------- invariants


def test_reservation_and_deployed_capital_invariants_through_a_lifecycle(e: OmsEnv) -> None:
    e.r.set_marks({"AAPL": "100.00"}, at=e.now())
    a = e.place().client_order_id
    b = e.place("AAPL", "100.00", "99.00").client_order_id
    assert a is not None and b is not None
    fill(e, a, "4")
    fill(e, b, "10", "100.00")
    sync(e, MIN)
    e.om.request_cancel(a, now=e.now())
    sync(e, MIN)
    bal = e.balances()
    assert bal.buy_commitments == 0
    assert bal.deployed_capital == bal.cost_basis_exposure == D(200) + D(1000)
    assert bal.deployed_capital <= bal.max_capital_allocation
    with e.db.read() as s:
        active = s.fetch_one("SELECT count(*) FROM cash_reservations WHERE status = 'ACTIVE'")
    assert active is not None and active[0] == 0
    assert e.r.env.pf.check_invariants(now=e.now()).ok
    assert not e.state().halted


# ---------------------------------------------------------- safety boundary


def test_no_alpaca_execution_and_simulate_only(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    pf, eng, gates = e.r.env.pf, e.r.engine, e.r.env.gates
    adapter = AlpacaPaperReadAdapter(FakeTransport(), clock=lambda: NOW, sleep=lambda _: None)
    with pytest.raises(ExecutionNotPermittedError):
        SimulationExecution(adapter)  # type: ignore[arg-type]
    with pytest.raises(ExecutionNotPermittedError):  # a raw SimBroker is not the capability
        OrderManager(e.db, pf, eng, gates, e.sim, e.sim, OMS_CONFIG)  # type: ignore[arg-type]
    observe = Database.create(tmp_path / "observe.sqlite3", RuntimeMode.OBSERVE, now=NOW)
    with pytest.raises(ExecutionNotPermittedError):
        OrderManager(observe, pf, eng, gates, e.sim, SimulationExecution(e.sim), OMS_CONFIG)
    with pytest.raises(ExecutionNotPermittedError):
        OrderManager(e.db, pf, eng, gates, e.sim, SimulationExecution(e.sim),
                     replace(OMS_CONFIG, mode=RuntimeMode.OBSERVE))  # fmt: skip
    observe.close()


def test_only_the_simulator_can_submit_or_cancel() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
    writers = set()
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name in {"submit_order",
                                                                   "cancel_order"}:  # fmt: skip
                writers.add(path.relative_to(src).as_posix())
    assert writers == {"broker/sim.py", "broker/ports.py"}  # ports.py: the Protocol only
    for path in (src / "oms").glob("*.py"):  # the OMS never imports the Alpaca adapter
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert "alpaca" not in (node.module or ""), path.name


def test_accept_script_initial_status_is_mapped(e: OmsEnv) -> None:
    e.sim.script_submit(Accept(initial_status="accepted"))
    r = e.place()
    assert r.status is S.ACCEPTED
