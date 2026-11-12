"""M1 step 12: crash safety at every transaction boundary, then restart recovery.

Two levels:

* in-process — a crash hook raises :class:`SimulatedCrash` (a BaseException), which
  unwinds like a dying process: the open transaction rolls back, nothing else runs;
* process-level — a spawned interpreter runs the OMS against the ledger file and calls
  ``os._exit`` at the boundary (no cleanup at all), while the parent keeps the broker.

After every crash a *new* set of collaborators performs restart recovery, which must
never submit (no duplicate broker order), must keep reservations for unresolved
orders, and must leave the ledger invariants intact.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.sim import AcceptThenFail, SimConfig
from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.ledger.portfolio import FillOutcome
from swing_trading_bot.ledger.repositories import OrdersRepository, ReservationStatus
from swing_trading_bot.oms.order_manager import CRASH_POINTS
from swing_trading_bot.oms.recovery import recover_after_restart
from tests.support.oms import OmsEnv, SimulatedCrash, crash_at, oms_env
from tests.support.oms_crash import CRASH_EXIT_CODE, reopen, run_child
from tests.support.portfolio import NOW

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)


def only_order(e: OmsEnv) -> str:
    with e.db.read() as s:
        rows = s.fetch_all("SELECT client_order_id FROM orders")
    assert len(rows) == 1
    return str(rows[0][0])


def restart(e: OmsEnv, after: timedelta = MIN) -> None:
    e.rebuild()  # new collaborators, no crash hook
    e.advance(after)
    recover_after_restart(e.recovery, e.sync, now=e.now())


def assert_safe(e: OmsEnv, coid: str) -> None:
    """After recovery: at most one broker order; reservation open iff still working."""
    assert len(e.submits(coid)) <= 1
    order = e.order(coid)
    res = e.reservation(coid)
    working = order.status not in (S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED,
                                   S.CONFIRMED_ABSENT)  # fmt: skip
    assert (res.status is ReservationStatus.ACTIVE) == working
    assert e.r.env.pf.check_invariants(now=e.now()).ok


# ------------------------------------------------------------ submission path


def test_crash_before_submission(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_intent_commit"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    o = e.order(coid)
    assert o.status is S.PENDING_SUBMIT and o.send_attempted_at is None
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert e.submits() == []
    restart(e)
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS  # resolved only via the protocol
    assert e.submits() == []  # never sent, never resent
    assert not e.r.gate(e.now()).new_entries_allowed
    assert_safe(e, coid)


def test_crash_after_send_marker_before_submit(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_send_marker"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    assert e.order(coid).send_attempted_at == NOW and e.submits() == []
    restart(e)
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS and e.submits() == []
    assert_safe(e, coid)


def test_crash_before_acknowledgment_persistence(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_submit"))
    with pytest.raises(SimulatedCrash):
        e.place()  # the broker accepted; the response was never written
    coid = only_order(e)
    assert e.order(coid).status is S.PENDING_SUBMIT and len(e.submits(coid)) == 1
    restart(e)
    o = e.order(coid)
    assert o.status is S.ACCEPTED and o.broker_order_id is not None  # adopted
    assert len(e.submits(coid)) == 1
    assert_safe(e, coid)


def test_crash_immediately_after_submission_with_delayed_visibility(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_submit"))
    e.sim.script_submit(AcceptThenFail("read_timeout", visibility_delay=timedelta(minutes=3)))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    restart(e)  # +1 min: not visible yet
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    restart(e, timedelta(minutes=3))  # another restart, now visible
    assert e.order(coid).status is S.ACCEPTED
    assert len(e.submits(coid)) == 1
    assert_safe(e, coid)


def test_crash_after_outcome_and_restart_is_a_no_op(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_outcome_commit"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    assert e.order(coid).status is S.ACCEPTED
    restart(e)
    assert e.order(coid).status is S.ACCEPTED and len(e.submits(coid)) == 1
    assert_safe(e, coid)


def test_crash_after_cancel_marker(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    coid = e.place().client_order_id
    assert coid is not None
    e.rebuild(crash_at("after_cancel_marker"))
    with pytest.raises(SimulatedCrash):
        e.om.request_cancel(coid, now=e.now())
    assert e.order(coid).status is S.PENDING_CANCEL  # recorded, never sent
    restart(e)
    # The broker never saw the cancel: the order is working again, reservation intact.
    assert e.order(coid).status is S.ACCEPTED
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert_safe(e, coid)


@pytest.mark.parametrize("point", ["after_activity", "after_fill", "after_cash",
                                   "after_position", "before_commit"])  # fmt: skip
def test_crash_during_fill_processing(tmp_path: Path, point: str) -> None:
    e = oms_env(tmp_path)
    coid = e.place().client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    e.rebuild(crash_at(f"fill:{point}"))
    e.advance(MIN)
    with pytest.raises(SimulatedCrash):
        e.sync.sync(now=e.now())
    with e.db.read() as s:  # the whole fill transaction rolled back
        assert s.fetch_one("SELECT count(*) FROM fills") == (0,)
        assert s.fetch_one("SELECT count(*) FROM broker_activities") == (0,)
    assert e.order(coid).filled_qty == 0
    restart(e)
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.FILLED, D(12))
    assert e.reservation(coid).status is ReservationStatus.CONSUMED
    again = e.sync.sync(now=e.now())
    assert set(again.fills.values()) <= {FillOutcome.DUPLICATE}  # booked exactly once
    assert_safe(e, coid)


def test_every_declared_crash_point_is_tested() -> None:
    tested = {"after_intent_commit", "send_marker_uncommitted", "after_send_marker",
              "after_submit", "after_outcome_commit", "after_cancel_marker"}  # fmt: skip
    assert set(CRASH_POINTS) == tested


def test_restart_recovery_never_submits(tmp_path: Path) -> None:
    e = oms_env(tmp_path, sim_config=SimConfig(order_visibility_delay=timedelta(minutes=40)))
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = e.place().client_order_id
    assert coid is not None
    for _ in range(5):  # repeated restarts while the order stays invisible
        restart(e, timedelta(minutes=5))
        assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    assert len(e.submits(coid)) == 1
    restart(e, timedelta(minutes=6))  # 31 minutes, still invisible: escalates
    assert e.state().reason_code == "AMBIGUOUS_ORDER_TIMEOUT"
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    restart(e, timedelta(minutes=10))  # past 40 minutes: visible, adopted
    assert e.order(coid).status is S.ACCEPTED
    assert e.state().reason_code == "AMBIGUOUS_ORDER_TIMEOUT"  # adoption never clears a halt
    assert len(e.submits(coid)) == 1


# ------------------------------------------------------------- process level


def _process_crash(tmp_path: Path, scenario: str, point: str,
                   prepare: object = None) -> tuple[OmsEnv, str]:  # fmt: skip
    e = oms_env(tmp_path)
    if callable(prepare):
        prepare(e)
    e.db.close()  # the parent holds no connection while the child runs
    result = run_child(e.sim, e.clock, e.r.env.db.path, scenario, point)
    assert result.exitcode == CRASH_EXIT_CODE, result
    e2 = reopen(e)
    return e2, only_order(e2)


def test_process_crash_before_submission(tmp_path: Path) -> None:
    e, coid = _process_crash(tmp_path, "place", "after_intent_commit")
    assert e.order(coid).status is S.PENDING_SUBMIT and e.submits() == []
    restart(e)
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS and e.submits() == []
    assert_safe(e, coid)


def test_process_crash_after_submission(tmp_path: Path) -> None:
    e, coid = _process_crash(tmp_path, "place", "after_submit")
    assert e.order(coid).status is S.PENDING_SUBMIT
    assert e.order(coid).send_attempted_at is not None and len(e.submits(coid)) == 1
    restart(e)
    assert e.order(coid).status is S.ACCEPTED and len(e.submits(coid)) == 1
    assert_safe(e, coid)


def test_process_crash_mid_fill_transaction(tmp_path: Path) -> None:
    holder: dict[str, str] = {}

    def prepare(e: OmsEnv) -> None:
        coid = e.place().client_order_id
        assert coid is not None
        holder["coid"] = coid
        e.sim.schedule_fill(coid, D(5), D(50), after=timedelta(seconds=5))
        e.advance(MIN)

    e, coid = _process_crash(tmp_path, "sync", "fill:after_position", prepare)
    assert coid == holder["coid"]
    with e.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM fills") == (0,)  # torn tx never landed
        assert OrdersRepository().require(s, coid).filled_qty == 0
    restart(e)
    o = e.order(coid)
    assert (o.status, o.filled_qty) == (S.PARTIALLY_FILLED, D(5))
    assert e.reservation(coid).remaining == D(350)
    assert_safe(e, coid)
