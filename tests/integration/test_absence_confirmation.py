"""Step 13.1: operator confirmation that an ambiguous order never reached the broker.

The successful path, then each prerequisite failing on its own. Timeline (minutes after
the send at T): lookups at +1, +5, +26; list and activity scans at +26; a real
reconciliation at +26m01s; confirmation one second later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    OrderNotFoundError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import FailBeforeReceipt
from swing_trading_bot.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from swing_trading_bot.ledger.repositories import (
    ReconciliationRunsRepository,
    ReservationStatus,
    ResolutionAttemptsRepository,
)
from swing_trading_bot.oms.recovery import AbsenceEvidence
from tests.support.portfolio import make_order, reserve
from tests.support.recon import ReconEnv, recon_env

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)


@dataclass
class Plan:
    lookups_at: tuple[int, ...] = (1, 5, 26)
    list_scan: bool = True
    activity_scan_at: int | None = 26
    reconcile: bool = True
    confirm_delay: timedelta = SEC
    before_lookups: Callable[[ReconEnv], None] | None = None
    before_list_scan: Callable[[ReconEnv], None] | None = None
    before_activity_scan: Callable[[ReconEnv], None] | None = None
    before_reconcile: Callable[[ReconEnv], None] | None = None
    notes: dict[str, object] = field(default_factory=dict)


def run_plan(r: ReconEnv, plan: Plan) -> tuple[str, AbsenceEvidence]:
    r.o.sim.script_submit(FailBeforeReceipt())
    r.o.advance(timedelta(0))
    coid = r.o.place().client_order_id
    assert coid is not None
    start = r.o.now()
    if plan.before_lookups:
        plan.before_lookups(r)
    for minute in plan.lookups_at:
        r.o.clock.advance_to(start + timedelta(minutes=minute))
        r.o.recovery.lookup(coid, now=r.o.now())
    r.o.clock.advance_to(max(r.o.now(), start + timedelta(minutes=26)))
    if plan.before_list_scan:
        plan.before_list_scan(r)
    if plan.list_scan:
        r.o.recovery.list_scan(coid, now=r.o.now())
    if plan.activity_scan_at is not None:
        if plan.before_activity_scan:
            plan.before_activity_scan(r)
        r.o.recovery.activity_scan(coid, now=start + timedelta(minutes=plan.activity_scan_at))
    if plan.before_reconcile:
        plan.before_reconcile(r)
    if plan.reconcile:
        plan.notes["report"] = r.run(SEC)
    ev = r.o.recovery.confirm_absent(coid, operator="alice", now=r.o.now() + plan.confirm_delay)
    return coid, ev


@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def refused(r: ReconEnv, coid: str, ev: AbsenceEvidence, expected: str) -> None:
    assert not ev.complete
    assert any(expected in m for m in ev.missing), ev.missing
    assert r.o.order(coid).status is S.SUBMIT_AMBIGUOUS  # nothing released
    assert r.o.reservation(coid).status is ReservationStatus.ACTIVE
    with r.o.db.read() as s:
        attempts = ResolutionAttemptsRepository().list_for_order(s, coid)
    assert attempts[-1].result == "REFUSED"  # every refusal is recorded with its reasons
    assert attempts[-1].detail is not None and attempts[-1].detail["missing"]


# --------------------------------------------------------------- success


def test_successful_confirmation(r: ReconEnv) -> None:
    plan = Plan()
    coid, ev = run_plan(r, plan)
    assert ev.complete, ev.missing
    assert ev.not_found_lookups == 3
    assert r.o.order(coid).status is S.CONFIRMED_ABSENT
    assert r.o.reservation(coid).status is ReservationStatus.RELEASED
    report = plan.notes["report"]
    amb = [f for f in report.findings if f.code == "AMBIGUOUS_ORDER_UNRESOLVED"]  # type: ignore[attr-defined]
    assert amb[0].detail_dict()["absence_consistent"] == "True"
    assert len(r.o.submits(coid)) == 1  # never resent


def test_404_lookups_are_evidence_not_proof(r: ReconEnv) -> None:
    def all_404(env: ReconEnv) -> None:
        for _ in range(3):
            env.o.sim.fail_next(
                "get_order_by_client_id", OrderNotFoundError("404", status_code=404)
            )

    _coid, ev = run_plan(r, Plan(before_lookups=all_404))
    assert ev.complete  # 404s count only as not-found lookups; everything else still required


def test_no_automatic_path_without_the_operator(r: ReconEnv) -> None:
    coid, _ = run_plan(r, Plan(reconcile=True, confirm_delay=SEC))
    r2_status = r.o.order(coid).status
    assert r2_status is S.CONFIRMED_ABSENT  # only because confirm_absent was called
    with pytest.raises(ValueError, match="operator"):
        r.o.recovery.confirm_absent(coid, operator="", now=r.o.now())


def test_cycles_alone_never_confirm(r: ReconEnv) -> None:
    r.o.sim.script_submit(FailBeforeReceipt())
    r.o.advance(timedelta(0))
    coid = r.o.place().client_order_id
    assert coid is not None
    for _ in range(40):
        r.o.recovery.cycle(now=r.o.clock.advance(MIN))
    assert r.o.order(coid).status is S.SUBMIT_AMBIGUOUS


# ------------------------------------------------- each failed prerequisite


def test_too_few_lookups(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(lookups_at=(5, 26)))
    refused(r, coid, ev, "2 of 3 not-found lookups")


def test_lookups_not_spread_over_ten_minutes(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(lookups_at=(20, 22, 26)))
    refused(r, coid, ev, "do not span the absence window")


def test_no_lookup_after_the_absence_window(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(lookups_at=(1, 5, 9)))
    refused(r, coid, ev, "no not-found lookup at or after")


def test_failed_lookups_do_not_count(r: ReconEnv) -> None:
    def fail_one(env: ReconEnv) -> None:
        env.o.sim.fail_next("get_order_by_client_id", ServiceUnavailableError("503"))

    coid, ev = run_plan(r, Plan(before_lookups=fail_one))
    refused(r, coid, ev, "2 of 3 not-found lookups")


def test_no_order_list_scan(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(list_scan=False))
    refused(r, coid, ev, "no clean order-list scan")


@pytest.mark.parametrize(
    "error", [ServiceUnavailableError("503"), IncompleteRetrievalError("saturated page")]
)
def test_incomplete_order_list_scan(r: ReconEnv, error: Exception) -> None:
    def fail(env: ReconEnv) -> None:
        env.o.sim.fail_next("get_orders", error)  # type: ignore[arg-type]

    coid, ev = run_plan(r, Plan(before_list_scan=fail))
    refused(r, coid, ev, "no clean order-list scan")


def test_no_activity_scan(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(activity_scan_at=None))
    refused(r, coid, ev, "no complete, clean activity scan")


def test_immature_activity_scan(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(activity_scan_at=20))  # closed window ends at +5 < +10
    refused(r, coid, ev, "no complete, clean activity scan")


def test_incomplete_activity_scan(r: ReconEnv) -> None:
    def fail(env: ReconEnv) -> None:
        env.o.sim.fail_next("get_activities", ServiceUnavailableError("503"))

    coid, ev = run_plan(r, Plan(before_activity_scan=fail))
    refused(r, coid, ev, "no complete, clean activity scan")


def test_unattributable_fill_in_the_activity_scan(r: ReconEnv) -> None:
    def stray_fill(env: ReconEnv) -> None:
        env.o.sim.submit_order(
            SubmitOrderRequest(
                "manual-5", "MSFT", Side.BUY, D(1), OrderType.LIMIT, TimeInForce.DAY, D(50)
            )
        )
        env.o.sim.schedule_fill("manual-5", D(1), D(50), after=SEC)

    coid, ev = run_plan(r, Plan(before_lookups=stray_fill))
    refused(r, coid, ev, "no complete, clean activity scan")


def test_no_reconciliation_run(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(reconcile=False))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_stale_reconciliation_run(r: ReconEnv) -> None:
    coid, ev = run_plan(r, Plan(confirm_delay=timedelta(minutes=11)))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_unrelated_inconclusive_finding(r: ReconEnv) -> None:
    def pending_fees(env: ReconEnv) -> None:
        env.o.sim.corrupt_next("get_account", lambda raw: {**raw, "pending_reg_taf_fees": "0.01"})

    coid, ev = run_plan(r, Plan(before_reconcile=pending_fees))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_unrelated_mismatch_finding(r: ReconEnv) -> None:
    def external_order(env: ReconEnv) -> None:
        env.o.sim.submit_order(
            SubmitOrderRequest(
                "manual-6", "AAPL", Side.BUY, D(1), OrderType.LIMIT, TimeInForce.DAY, D(100)
            )
        )

    coid, ev = run_plan(r, Plan(before_reconcile=external_order))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_another_unresolved_order_disqualifies(r: ReconEnv) -> None:
    def second_ambiguous(env: ReconEnv) -> None:
        other = make_order(env.o.db, symbol="ZZZ", qty="1", limit="20.00")
        reserve(env.o.r.env.pf, env.o.db, other)  # PENDING_SUBMIT: unresolved

    coid, ev = run_plan(r, Plan(before_reconcile=second_ambiguous))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_synthetic_clean_run_is_not_absence_evidence(r: ReconEnv) -> None:
    def clean_row(env: ReconEnv) -> None:
        with env.o.db.transaction() as tx:
            from swing_trading_bot.domain.enums import ReconciliationResult  # noqa: PLC0415

            ReconciliationRunsRepository().append(
                tx, ReconciliationResult.OK, None, env.o.now() + SEC
            )

    coid, ev = run_plan(r, Plan(reconcile=False, before_reconcile=clean_row))
    refused(r, coid, ev, "no clean, fresh reconciliation run")


def test_reconciliation_too_early_is_not_mature(r: ReconEnv) -> None:
    r.o.sim.script_submit(FailBeforeReceipt())
    r.o.advance(timedelta(0))
    r.o.place()
    report = r.run(timedelta(minutes=12))  # closed history ends 3 minutes before +10
    amb = [f for f in report.findings if f.code == "AMBIGUOUS_ORDER_UNRESOLVED"]
    assert amb[0].detail_dict()["absence_consistent"] == "False"
    assert "activity coverage not mature" in amb[0].detail_dict()["not_consistent_because"]


def test_status_must_be_submit_ambiguous(r: ReconEnv) -> None:
    order = make_order(r.o.db, symbol="ZZZ", qty="1", limit="20.00")
    reserve(r.o.r.env.pf, r.o.db, order)
    ev = r.o.recovery.confirm_absent(order.client_order_id, operator="op", now=r.o.now())
    assert any("not SUBMIT_AMBIGUOUS" in m for m in ev.missing)
    assert r.o.order(order.client_order_id).status is S.PENDING_SUBMIT
