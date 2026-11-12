"""M1 step 12: failure paths — broker read faults, malformed payloads, unmapped or
inconsistent broker states, cash activities, and recovery-protocol edge cases."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    NotCancelableError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.models import ActivityPage, BrokerActivity, SubmitOrderRequest
from swing_trading_bot.broker.sim import Accept, AcceptThenFail, FailBeforeReceipt, SimBroker
from swing_trading_bot.domain.enums import HaltReason, OrderStatus, Side
from swing_trading_bot.ledger.repositories import ReservationStatus
from swing_trading_bot.oms.execution import register_simulation_double
from swing_trading_bot.oms.order_manager import (
    CancelOutcome,
    ExecutionNotPermittedError,
    RefusalCode,
    check_execution_allowed,
)
from swing_trading_bot.oms.recovery import AttemptResult
from swing_trading_bot.oms.sync import OrderSync, SyncStatus
from tests.support.oms import OMS_CONFIG, OmsEnv, oms_env

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)


@pytest.fixture
def e(tmp_path: Path) -> OmsEnv:
    return oms_env(tmp_path)


def placed(e: OmsEnv) -> str:
    coid = e.place().client_order_id
    assert coid is not None
    return coid


def corrupt_lookup(e: OmsEnv, **changes: Any) -> None:
    e.sim.corrupt_next("get_order_by_client_id", lambda raw: {**raw, **changes})


# ------------------------------------------------------------ activity reads


def test_transient_activity_read_failure_is_incomplete_not_halted(e: OmsEnv) -> None:
    e.sim.fail_next("get_activities", ServiceUnavailableError("503"))
    report = e.sync.sync(now=e.advance(MIN))
    assert report.status is SyncStatus.INCOMPLETE and not report.coverage_advanced
    assert not e.state().halted


def test_activity_auth_failure_halts(e: OmsEnv) -> None:
    e.sim.fail_next("get_activities", AuthenticationError("401", status_code=401))
    assert e.sync.sync(now=e.advance(MIN)).status is SyncStatus.HALTED
    assert e.state().reason_code == HaltReason.AUTH


def test_float_or_malformed_activity_payload_halts(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_fill(coid, D(2), D(50), after=timedelta(seconds=5))
    e.sim.corrupt_next("get_activities", lambda raw: {**raw, "qty": 2.0})  # a float
    assert e.sync.sync(now=e.advance(MIN)).status is SyncStatus.HALTED
    assert e.state().reason_code == HaltReason.BROKER_PAYLOAD


def test_cash_activities_are_classified_before_booking(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    e.sync.sync(now=e.advance(MIN))
    e.sim.schedule_cash_activity("FEE", D("-0.02"), after=timedelta(seconds=5),
                                 order_client_id=coid, description="TAF")  # fmt: skip
    e.sim.schedule_cash_activity("FEE", D("-0.03"), after=timedelta(seconds=5))  # no order ref
    e.sim.schedule_cash_activity("INT", D("0.40"), after=timedelta(seconds=7))  # external
    e.sim.schedule_cash_activity(
        "DIV",
        D("2.40"),
        symbol="MSFT",
        qty=D(12),
        per_share_amount=D("0.20"),
        after=timedelta(seconds=8),
    )
    report = e.sync.sync(now=e.advance(MIN))
    assert report.cash_activities == 1  # only the order-referenced fee is booked
    assert len(report.unclassified) == 2  # the unreferenced fee; the just-bought dividend
    assert e.balances().cash == D(5000) - D(600) - D("0.02")
    with e.db.read() as s:
        sql = ("SELECT activity_type, attribution FROM broker_activities "
               "WHERE activity_type <> 'FILL' ORDER BY ts, activity_id")  # fmt: skip
        rows = s.fetch_all(sql)
    assert sorted(rows) == sorted([("FEE", "ATTRIBUTABLE"), ("FEE", "UNEXPLAINED"),
                                   ("INT", "EXTERNAL"), ("DIV", "UNEXPLAINED")])  # fmt: skip
    again = e.sync.sync(now=e.now())  # replay: nothing booked twice, no false conflict
    assert again.status is not SyncStatus.HALTED
    assert e.balances().cash == D(5000) - D(600) - D("0.02")


def test_unclassified_activity_is_preserved(e: OmsEnv) -> None:
    e.sim.schedule_cash_activity("FEE", D("-0.02"), after=timedelta(seconds=5))
    e.sim.corrupt_next("get_activities", lambda raw: {**raw, "activity_type": "JNLC"})
    report = e.sync.sync(now=e.advance(MIN))
    assert len(report.unclassified) == 1 and report.cash_activities == 0
    with e.db.read() as s:
        row = s.fetch_one("SELECT activity_type, attribution FROM broker_activities")
    assert row == ("JNLC", "UNEXPLAINED")
    assert e.balances().cash == D(5000)  # preserved, not booked


class ScriptedActivities:
    """Delegates to the simulator but serves a fixed activity list (shapes no adapter
    produces today, to exercise defensive branches)."""

    def __init__(self, sim: SimBroker, activities: tuple[BrokerActivity, ...]) -> None:
        self._sim, self._activities = sim, activities

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    def get_activities(self, **_: Any) -> ActivityPage:
        return ActivityPage(self._activities, page_size=100)


def test_incomplete_fill_record_is_preserved_and_halts(e: OmsEnv) -> None:
    bad = BrokerActivity("x-1", "FILL", date(2026, 11, 9), None, '{"id":"x-1"}', symbol="MSFT")
    sync = OrderSync(e.db, e.r.env.pf, ScriptedActivities(e.sim, (bad,)), OMS_CONFIG)
    assert sync.sync(now=e.advance(MIN)).status is SyncStatus.HALTED
    assert e.state().reason_code == HaltReason.BROKER_PAYLOAD
    with e.db.read() as s:
        assert s.fetch_one("SELECT attribution FROM broker_activities") == ("UNEXPLAINED",)


def test_fill_for_an_unadopted_ambiguous_order_adopts_it_first(e: OmsEnv) -> None:
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = placed(e)
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    report = e.sync.sync(now=e.advance(MIN))  # sync, not the recovery protocol, sees it
    assert report.adopted == [coid]
    assert e.order(coid).status is S.FILLED
    assert e.reservation(coid).status is ReservationStatus.CONSUMED


def test_order_list_failure_during_attribution_is_incomplete(e: OmsEnv) -> None:
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = placed(e)
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    e.sim.fail_next("get_orders", ServiceUnavailableError("503"))
    report = e.sync.sync(now=e.advance(MIN))
    assert report.status is SyncStatus.INCOMPLETE and report.fills == {}
    assert e.order(coid).filled_qty == 0  # nothing booked against a guess


# --------------------------------------------------------------- order reads


def test_order_refresh_failure_and_invisible_order(e: OmsEnv) -> None:
    e.sim.script_submit(Accept(visibility_delay=timedelta(minutes=5)))
    coid = placed(e)  # adopted from the submit response
    report = e.sync.sync(now=e.advance(MIN))
    assert report.not_visible == [coid] and e.order(coid).status is S.ACCEPTED
    e.sim.fail_next("get_order_by_client_id", ServiceUnavailableError("503"))
    assert e.sync.sync(now=e.advance(timedelta(minutes=5))).status is SyncStatus.INCOMPLETE


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "mystery"}, HaltReason.UNMAPPED_BROKER_STATUS),
        ({"status": "replaced"}, HaltReason.UNEXPECTED_BROKER_STATUS),
        ({"status": "partially_filled", "filled_qty": "0"}, HaltReason.BROKER_PAYLOAD),
        ({"qty": "13"}, HaltReason.RECON_ORDER),
        ({"limit_price": "51.00"}, HaltReason.RECON_ORDER),
    ],
)
def test_unsafe_broker_order_states_halt(e: OmsEnv, changes: dict[str, str],
                                         reason: HaltReason) -> None:  # fmt: skip
    coid = placed(e)
    corrupt_lookup(e, **changes)
    assert e.sync.sync(now=e.advance(MIN)).status is SyncStatus.HALTED
    assert e.state().reason_code == reason
    assert e.order(coid).status is S.ACCEPTED  # left untouched
    assert e.reservation(coid).status is ReservationStatus.ACTIVE


def test_booked_fills_exceeding_broker_filled_qty_halts(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_fill(coid, D(5), D(50), after=timedelta(seconds=5))
    e.sync.sync(now=e.advance(MIN))
    corrupt_lookup(e, filled_qty="0", status="new", filled_avg_price=None)
    e.sync.sync(now=e.advance(MIN))
    assert e.state().reason_code == HaltReason.RECON_ORDER


def test_illegal_transition_from_a_terminal_order_halts(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_broker_cancel(coid, after=timedelta(seconds=5))
    e.sync.sync(now=e.advance(MIN))
    assert e.order(coid).status is S.CANCELED
    corrupt_lookup(e, status="new")
    e.recovery.lookup(coid, now=e.advance(MIN))
    assert e.state().reason_code == HaltReason.ILLEGAL_TRANSITION
    assert e.order(coid).status is S.CANCELED


# --------------------------------------------------------------- cancellation


def test_cancel_edge_cases(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    e.advance(MIN)  # filled at the broker; local still ACCEPTED
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.ALREADY_TERMINAL
    assert e.order(coid).status is S.PENDING_CANCEL  # recorded; sync resolves it
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    e.sync.sync(now=e.now())
    assert e.order(coid).status is S.FILLED
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.ALREADY_TERMINAL


@register_simulation_double
class FailingCancel:
    SIMULATION_DOUBLE_PURPOSE = "cancel transport failure against a SimBroker"

    def __init__(self, sim: SimBroker) -> None:
        self._sim = sim

    def submit_order(self, request: SubmitOrderRequest) -> Any:
        return self._sim.submit_order(request)

    def cancel_order(self, broker_order_id: str) -> None:
        raise ServiceUnavailableError("503")


def test_cancel_send_failure_keeps_everything(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    e.execution = FailingCancel(e.sim)
    e.rebuild()
    coid = placed(e)
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.SEND_FAILED
    assert e.order(coid).status is S.PENDING_CANCEL
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    e.sync.sync(now=e.advance(MIN))  # broker never canceled: working again
    assert e.order(coid).status is S.ACCEPTED


def test_not_cancelable_at_broker(e: OmsEnv) -> None:
    coid = placed(e)
    e.sim.schedule_broker_cancel(coid, after=timedelta(seconds=1))
    e.advance(MIN)
    assert e.om.request_cancel(coid, now=e.now()) is CancelOutcome.ALREADY_TERMINAL
    e.sync.sync(now=e.now())
    assert e.order(coid).status is S.CANCELED
    assert e.reservation(coid).status is ReservationStatus.RELEASED
    _ = NotCancelableError


# ------------------------------------------------------------ submission edge


def test_account_unavailable_refuses_before_writing(e: OmsEnv) -> None:
    d = e.decide()
    e.sim.fail_next("get_account", ServiceUnavailableError("503"))
    r = e.om.place_entry(d, now=e.now())
    assert r.refusal is RefusalCode.ACCOUNT_UNAVAILABLE and e.submits() == []
    with e.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM orders") == (0,)


def test_non_port_execution_is_refused(e: OmsEnv) -> None:
    with pytest.raises(ExecutionNotPermittedError):
        check_execution_allowed(e.db, object())


# ----------------------------------------------------------- recovery edges


def test_lookup_errors_are_recorded_not_inferred(e: OmsEnv) -> None:
    e.sim.script_submit(FailBeforeReceipt())
    coid = placed(e)
    e.sim.fail_next("get_order_by_client_id", ServiceUnavailableError("503"))
    assert e.recovery.lookup(coid, now=e.advance(MIN)) is S.SUBMIT_AMBIGUOUS
    e.sim.fail_next("get_order_by_client_id", AuthenticationError("401", status_code=401))
    e.recovery.lookup(coid, now=e.advance(MIN))
    assert e.state().reason_code == HaltReason.AUTH
    ev = e.recovery.confirm_absent(coid, operator="op", now=e.now())
    assert "0 of 3 not-found lookups" in ev.missing  # errors are not evidence
    with pytest.raises(ValueError, match="operator"):
        e.recovery.confirm_absent(coid, operator=" ", now=e.now())


def test_scan_edge_cases(e: OmsEnv) -> None:
    e.sim.script_submit(FailBeforeReceipt())
    coid = placed(e)
    assert e.recovery.activity_scan(coid, now=e.advance(MIN)) is AttemptResult.INCOMPLETE
    e.sim.fail_next("get_orders", ServiceUnavailableError("503"))
    assert e.recovery.list_scan(coid, now=e.now()) is AttemptResult.ERROR
    e.advance(timedelta(minutes=30))
    e.sim.fail_next("get_activities", ServiceUnavailableError("503"))
    assert e.recovery.activity_scan(coid, now=e.now()) is AttemptResult.INCOMPLETE
    # a fill nobody can attribute blocks any absence finding
    stray = "manual-2"
    e.sim.submit_order(SubmitOrderRequest(stray, "MSFT", Side.BUY, D(1), e.order(coid).order_type,
                                          e.order(coid).time_in_force, D("50.00")))  # fmt: skip
    e.sim.schedule_fill(stray, D(1), D(50), after=timedelta(seconds=1))
    e.advance(timedelta(minutes=16))
    assert e.recovery.activity_scan(coid, now=e.now()) is AttemptResult.UNATTRIBUTED_FILLS


def test_list_scan_finds_and_adopts(e: OmsEnv) -> None:
    e.sim.script_submit(AcceptThenFail("gateway_timeout", visibility_delay=timedelta(minutes=2)))
    coid = placed(e)
    e.sim.fail_next("get_order_by_client_id", ServiceUnavailableError("503"))
    e.advance(timedelta(minutes=3))
    assert e.recovery.list_scan(coid, now=e.now()) is AttemptResult.FOUND
    assert e.order(coid).status is S.ACCEPTED


def test_malformed_lookup_payload_halts_instead_of_counting(e: OmsEnv) -> None:
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = placed(e)
    corrupt_lookup(e, filled_qty=0.0)  # a float where a decimal string is required
    assert e.recovery.lookup(coid, now=e.advance(MIN)) is S.SUBMIT_AMBIGUOUS
    assert e.state().reason_code == HaltReason.BROKER_PAYLOAD
