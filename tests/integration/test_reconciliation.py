"""M1 step 13: broker reconciliation (SimBroker + temporary ledgers only)."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import AcceptThenFail, FailBeforeReceipt, SimBroker, SimConfig
from swing_trading_bot.domain.enums import (
    HaltReason,
    OrderStatus,
    OrderType,
    ReconciliationResult,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    ReconciliationRunsRepository,
    ReservationStatus,
    TradingStateRepository,
)
from swing_trading_bot.oms.reconciler import (
    Outcome,
    Severity,
    resume_after_clean_reconciliation,
    run_findings,
)
from swing_trading_bot.risk.gates import GateReason
from tests.support.oms import SimulatedCrash, crash_at
from tests.support.portfolio import NOW, make_order, reserve
from tests.support.recon import ReconEnv, recon_env

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)
QUIET = timedelta(minutes=20)  # past the 15-minute posting lag


@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def placed(r: ReconEnv, symbol: str = "MSFT", entry: str = "50.00", stop: str = "48.00") -> str:
    r.o.advance(timedelta(0), marks={symbol: entry})
    coid = r.o.place(symbol, entry, stop).client_order_id
    assert coid is not None
    return coid


def filled(r: ReconEnv, qty: str = "12", **kw: Any) -> str:
    coid = placed(r)
    r.o.sim.schedule_fill(coid, D(qty), D(50), after=timedelta(seconds=5), **kw)
    return coid


def halted(r: ReconEnv) -> str | None:
    return r.o.state().reason_code if r.o.state().halted else None


def bot_order_at_broker(sim: SimBroker, coid: str, *, qty: str = "2") -> None:
    sim.submit_order(SubmitOrderRequest(coid, "MSFT", Side.BUY, D(qty), OrderType.LIMIT,
                                        TimeInForce.DAY, D("50.00")))  # fmt: skip


# ======================================================= matching & baselines


def test_matching_state_is_clean_and_persisted(r: ReconEnv) -> None:
    coid = filled(r)
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.CLEAN and rep.findings == () and rep.halted is None
    assert r.o.order(coid).status is S.FILLED
    with r.o.db.read() as s:
        run = ReconciliationRunsRepository().latest(s)
        assert run is not None and run.id == rep.run_id
        assert run.result is ReconciliationResult.OK
        assert run_findings(s, rep.run_id) == []
    windows = {w["kind"]: w for w in rep.evidence["windows"]}
    assert (
        windows["FULL_HISTORY"]["complete"] and windows["FULL_HISTORY"]["after"] == NOW.isoformat()
    )
    assert rep.evidence["orders_scan"]["count"] == 1 and rep.evidence["head_stable"]


def test_broker_cash_differs_from_virtual_but_is_explained(r: ReconEnv) -> None:
    filled(r)
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.CLEAN
    assert rep.evidence["account"]["cash"] == "99400"  # $100,000 paper account
    assert rep.evidence["baseline"]["cash"] == "100000"
    assert r.o.balances().cash == D(4400)  # $5,000 virtual allocation
    assert r.o.balances().max_capital_allocation == D(5000)  # never raised


def test_unexplained_broker_cash_is_a_mismatch(r: ReconEnv) -> None:
    filled(r)
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "cash": "99400.01"})
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.MISMATCH and "CASH_UNEXPLAINED" in rep.codes()
    assert rep.halted is HaltReason.RECON_CASH and halted(r) == "RECON_CASH"


def test_no_baseline_is_inconclusive(tmp_path: Path) -> None:
    from tests.support.oms import oms_env  # noqa: PLC0415

    r = ReconEnv(oms_env(tmp_path))
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.INCONCLUSIVE and "BASELINE_MISSING" in rep.codes()


# =================================================================== orders


def test_missing_broker_order(r: ReconEnv) -> None:
    order = make_order(r.o.db, symbol="ZZZ", qty="2", limit="20.00")
    reserve(r.o.r.env.pf, r.o.db, order)
    with r.o.db.transaction() as tx:
        repo = OrdersRepository()
        repo.set_broker_order_id(tx, order.client_order_id, "ghost-broker-id", NOW)
        repo.transition_status(tx, order.client_order_id, expected=S.PENDING_SUBMIT,
                               target=S.ACCEPTED, at=NOW)  # fmt: skip
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.MISMATCH and "MISSING_BROKER_ORDER" in rep.codes()
    assert rep.halted is HaltReason.RECON_ORDER
    assert r.o.reservation(order.client_order_id).status is ReservationStatus.ACTIVE


def test_unexpected_and_external_broker_orders(r: ReconEnv) -> None:
    bot_order_at_broker(r.o.sim, new_client_order_id())  # bot-shaped, unknown locally
    bot_order_at_broker(r.o.sim, "manual-123")  # placed by hand in the paper account
    rep = r.run(QUIET)
    assert {"UNEXPECTED_ORDER", "EXTERNAL_ORDER", "MULTIPLE_WORKING_ORDERS"} <= rep.codes(
        Severity.MISMATCH
    )
    assert halted(r) == "RECON_ORDER"


class DuplicatingOrders:
    def __init__(self, sim: SimBroker) -> None:
        self._sim = sim

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    def get_orders(self, **kw: Any) -> Any:
        listed = self._sim.get_orders(**kw)
        return listed + listed[:1]


def test_duplicate_broker_order_records(r: ReconEnv) -> None:
    placed(r)
    r.o.read = DuplicatingOrders(r.o.sim)
    rep = r.run(QUIET)
    assert "DUPLICATE_BROKER_ORDER" in rep.codes(Severity.MISMATCH)


def test_partial_fill_with_pending_cancellation(tmp_path: Path) -> None:
    r = recon_env(tmp_path, sim_config=SimConfig(cancel_delay=timedelta(minutes=30)))
    coid = filled(r, "5")
    r.o.sync.sync(now=r.o.clock.advance(MIN))
    r.o.om.request_cancel(coid, now=r.o.now())
    rep = r.run(QUIET)  # broker: pending_cancel, 5 filled; ledger: PENDING_CANCEL, 5 booked
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert r.o.order(coid).status is S.PENDING_CANCEL
    assert r.o.reservation(coid).status is ReservationStatus.ACTIVE
    rep = r.run(timedelta(minutes=15))  # cancel confirmed
    assert rep.outcome is Outcome.CLEAN and r.o.order(coid).status is S.CANCELED
    assert r.o.reservation(coid).status is ReservationStatus.RELEASED


def test_ambiguous_submission_with_delayed_visibility(r: ReconEnv) -> None:
    r.o.sim.script_submit(AcceptThenFail("gateway_timeout", visibility_delay=timedelta(minutes=8)))
    coid = placed(r)
    rep = r.run(2 * MIN)
    assert rep.outcome is Outcome.INCONCLUSIVE and rep.halted is None
    assert "AMBIGUOUS_ORDER_UNRESOLVED" in rep.codes()
    assert r.o.order(coid).status is S.SUBMIT_AMBIGUOUS  # a 404/absence is not proof
    rep = r.run(timedelta(minutes=7))  # visible now: adopted from the order list
    assert r.o.order(coid).status is S.ACCEPTED
    assert rep.outcome is Outcome.CLEAN, rep.findings


# ======================================================= executions & lags


def test_missing_fill_activity_and_posting_beyond_the_lag(r: ReconEnv) -> None:
    coid = filled(r, activity_delay=timedelta(minutes=40))  # far beyond the 15-min lag
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.INCONCLUSIVE and rep.halted is None
    assert {"FILL_ACTIVITY_LAG", "POSITION_QTY_MISMATCH", "CASH_RESIDUAL_PENDING"} <= rep.codes(
        Severity.INCONCLUSIVE
    )
    assert r.o.order(coid).status is not S.FILLED
    assert r.o.reservation(coid).status is ReservationStatus.ACTIVE
    assert GateReason.UNBOOKED_FILLS.value in r.o.r.gate(r.o.now()).reason_codes
    rep = r.run(timedelta(minutes=30))
    assert rep.outcome is Outcome.CLEAN and r.o.order(coid).status is S.FILLED


def test_late_posted_activity_found_only_by_historical_rescan(r: ReconEnv) -> None:
    coid = filled(r)
    r.o.sim.schedule_cash_activity("FEE", D("-0.02"), after=timedelta(seconds=30),
                                   activity_delay=timedelta(hours=3),
                                   order_client_id=coid)  # fmt: skip
    for _ in range(9):  # forward syncs every 20 minutes move the coverage marker on
        r.o.sync.sync(now=r.o.clock.advance(QUIET))
    r.o.clock.advance(QUIET)  # 3h20m: posted, but before the forward overlap window
    forward = r.o.sync.sync(now=r.o.now())
    assert forward.cash_activities == 0  # forward progress alone misses it
    rep = r.run(MIN)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    with r.o.db.read() as s:
        row = s.fetch_one("SELECT attribution FROM broker_activities WHERE activity_type='FEE'")
    assert row == ("ATTRIBUTABLE",)
    assert r.o.balances().cash == D(4400) - D("0.02")


def test_duplicate_and_conflicting_activities(r: ReconEnv) -> None:
    filled(r)
    first = r.run(QUIET)
    again = r.run(MIN)  # every activity re-read: duplicates are no-ops
    assert first.outcome is again.outcome is Outcome.CLEAN
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM fills") == (1,)
    r.o.sim.corrupt_next("get_activities", lambda raw: {**raw, "price": "49.99"})
    rep = r.run(MIN)
    assert rep.outcome is Outcome.MISMATCH and "ACTIVITY_CONFLICT" in rep.codes()
    assert halted(r) == "RECON_EXECUTION"
    with r.o.db.read() as s:  # the booked fill was never rewritten
        price = s.fetch_one("SELECT price FROM fills")
    assert price is not None and Decimal(str(price[0])) == D(50)


def test_out_of_order_fill(r: ReconEnv) -> None:
    coid = placed(r)
    r.o.sim.schedule_fill(coid, D(5), D(50), after=timedelta(seconds=5),
                          activity_delay=timedelta(minutes=20))  # fmt: skip
    r.o.sim.schedule_fill(coid, D(7), D(50), after=timedelta(seconds=10))
    r.o.sync.sync(now=r.o.clock.advance(MIN))
    rep = r.run(timedelta(minutes=25))
    assert rep.outcome is Outcome.MISMATCH
    assert {"OUT_OF_ORDER_BOOKING"} <= rep.codes(Severity.MISMATCH)
    assert r.o.order(coid).filled_qty == D(12)  # both recorded before halting


def test_manual_external_trade(r: ReconEnv) -> None:
    bot_order_at_broker(r.o.sim, "manual-1", qty="3")
    r.o.sim.schedule_fill("manual-1", D(3), D(50), after=timedelta(seconds=5))
    rep = r.run(QUIET)
    assert {"EXTERNAL_ORDER", "UNEXPLAINED_FILL", "POSITION_QTY_MISMATCH"} <= rep.codes(
        Severity.MISMATCH
    )
    assert rep.halted is HaltReason.RECON_EXECUTION
    with r.o.db.read() as s:  # preserved before the halt
        assert s.fetch_one("SELECT attribution FROM broker_activities") == ("UNEXPLAINED",)


@pytest.mark.parametrize(("kind", "amount", "symbol"),
                         [("FEE", "-1.00", None), ("DIV", "2.00", "AAPL"),
                          ("CSD", "250.00", None), ("JNLC", "-10.00", None)])  # fmt: skip
def test_unexpected_cash_activity(r: ReconEnv, kind: str, amount: str,
                                  symbol: str | None) -> None:  # fmt: skip
    r.o.sim.schedule_cash_activity(kind, D(amount), symbol=symbol,  # type: ignore[arg-type]
                                   after=timedelta(seconds=5))  # fmt: skip
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.MISMATCH
    expected = "DIVIDEND_ENTITLEMENT_UNVERIFIED" if kind == "DIV" else "UNEXPLAINED_CASH_ACTIVITY"
    assert expected in rep.codes(Severity.MISMATCH)
    assert "CASH_UNEXPLAINED" not in rep.codes()  # broker cash itself is explained
    assert rep.halted is HaltReason.RECON_CASH
    assert r.o.balances().cash == D(5000)  # never booked to the virtual ledger


def test_interest_is_external_and_explained(r: ReconEnv) -> None:
    r.o.sim.schedule_cash_activity("INT", D("3.21"), after=timedelta(seconds=5))
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.CLEAN and r.o.balances().cash == D(5000)


# ================================================================ retrieval


def test_incomplete_order_pagination(r: ReconEnv) -> None:
    filled(r)
    r.o.sim.fail_next("get_orders", IncompleteRetrievalError("500 orders at one timestamp"))
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.INCONCLUSIVE and rep.halted is None
    assert "INCOMPLETE_RETRIEVAL" in rep.codes()
    assert not rep.codes(Severity.MISMATCH)  # nothing judged from a partial list


def test_incomplete_activity_pagination(r: ReconEnv) -> None:
    filled(r)
    r.run(QUIET)
    for _ in range(3):  # every activity read of this run fails mid-pagination
        r.o.sim.fail_next("get_activities", ServiceUnavailableError("503"))
    rep = r.run(MIN)
    assert rep.outcome is Outcome.INCONCLUSIVE and rep.halted is None
    retrieval = {"SYNC_INCOMPLETE", "ACTIVITY_WINDOW_INCOMPLETE", "ACTIVITY_HEAD_UNSTABLE",
                 "CASH_NOT_EVALUATED"}  # fmt: skip
    assert rep.codes(Severity.INCONCLUSIVE) & retrieval


def test_failed_read_does_not_hide_a_confirmed_mismatch(r: ReconEnv) -> None:
    bot_order_at_broker(r.o.sim, "manual-9")
    r.o.sim.fail_next("get_positions", ServiceUnavailableError("503"))
    rep = r.run(QUIET)
    assert "READ_FAILED" in rep.codes(Severity.INCONCLUSIVE)
    assert rep.outcome is Outcome.MISMATCH  # the external order is independently confirmed


def test_unknown_settlement_date(r: ReconEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    pf = r.o.r.env.pf
    monkeypatch.setattr(pf, "_settlement_date", lambda *_a, **_k: None)  # calendar gap
    filled(r)
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.INCONCLUSIVE
    assert "SETTLEMENT_DATE_UNKNOWN" in rep.codes(Severity.INCONCLUSIVE)
    monkeypatch.undo()  # the calendar is available again: resolved, never guessed
    rep = r.run(MIN)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM settlement_resolutions") == (1,)


def test_pending_fees_and_account_identity(r: ReconEnv) -> None:
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "pending_reg_taf_fees": "0.02"})
    assert "FEES_PENDING" in r.run(QUIET).codes(Severity.INCONCLUSIVE)
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "id": "another-account"})
    rep = r.run(MIN)
    assert "ACCOUNT_ID_MISMATCH" in rep.codes(Severity.MISMATCH)
    assert rep.halted is HaltReason.RECON_ACCOUNT


# ===================================================== replay, restart, halts


def test_repeated_identical_reconciliation(r: ReconEnv) -> None:
    filled(r)
    a = r.run(QUIET)
    b = r.run(timedelta(seconds=1))
    assert (a.outcome, a.findings) == (b.outcome, b.findings)
    assert a.evidence["broker_snapshot_hash"] == b.evidence["broker_snapshot_hash"]
    assert b.run_id == a.run_id + 1
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM fills") == (1,)
        assert s.fetch_one("SELECT count(*) FROM halt_events WHERE actor = 'reconciler'") == (0,)


@pytest.mark.parametrize("point", ["recon:after_observation", "recon:before_persist"])
def test_restart_during_reconciliation(r: ReconEnv, point: str) -> None:
    filled(r)
    r.o.clock.advance(QUIET)
    with r.o.db.read() as s:
        runs_before = s.fetch_one("SELECT count(*) FROM reconciliation_runs")
    with pytest.raises(SimulatedCrash):
        r.reconciler(crash_at(point)).run(now=r.o.now())
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM reconciliation_runs") == runs_before
        assert s.fetch_one("SELECT count(*) FROM fills") == (1,)  # ingestion committed
    rep = r.reconciler().run(now=r.o.now())  # the "restarted" process
    assert rep.outcome is Outcome.CLEAN
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM fills") == (1,)


def test_crash_while_booking_during_reconciliation(r: ReconEnv) -> None:
    filled(r)
    r.o.clock.advance(QUIET)
    with pytest.raises(SimulatedCrash):
        r.reconciler(crash_at("fill:after_cash")).run(now=r.o.now())
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM fills") == (0,)  # torn tx rolled back
    assert r.reconciler().run(now=r.o.now()).outcome is Outcome.CLEAN


def test_mismatch_halt_survives_restart_and_resume_needs_fresh_clean_run(r: ReconEnv) -> None:
    filled(r)
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "cash": "99399.99"})
    assert r.run(QUIET).halted is HaltReason.RECON_CASH
    r.o.rebuild()  # restart: the halt is persisted
    assert halted(r) == "RECON_CASH"
    db = r.o.db
    assert not resume_after_clean_reconciliation(db, actor="op", detail="try", now=r.o.now(),
                                                 max_age=timedelta(minutes=10))  # fmt: skip
    clean = r.run(MIN)  # the transient discrepancy is gone
    assert clean.outcome is Outcome.CLEAN
    late = r.o.now() + timedelta(minutes=11)
    assert not resume_after_clean_reconciliation(db, actor="op", detail="stale", now=late,
                                                 max_age=timedelta(minutes=10))  # fmt: skip
    assert halted(r) == "RECON_CASH"
    assert resume_after_clean_reconciliation(db, actor="op", detail="ok", now=r.o.now(),
                                             max_age=timedelta(minutes=10))  # fmt: skip
    assert halted(r) is None
    with db.read() as s:
        actions = [e.action for e in TradingStateRepository().events(s)]
    assert actions[-3:] == ["RESUME_REFUSED", "RESUME_REFUSED", "RESUME"]


def test_consecutive_inconclusive_runs_escalate(r: ReconEnv) -> None:
    filled(r, activity_delay=timedelta(hours=5))  # stays INCONCLUSIVE for hours
    results = [r.run(QUIET if i == 0 else MIN) for i in range(5)]
    assert [x.outcome for x in results] == [Outcome.INCONCLUSIVE] * 5
    assert [x.halted for x in results] == [None] * 4 + [HaltReason.RECON_INCONCLUSIVE_REPEAT]
    assert r.run(MIN).halted is None  # escalated once, not on every further run


def test_gate_uses_reconciliation_and_staleness(r: ReconEnv) -> None:
    filled(r)
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.CLEAN
    codes = set(r.o.r.gate(r.o.now()).reason_codes)
    assert not {"RECONCILIATION_STALE", "RECONCILIATION_INCONCLUSIVE",
                "RECONCILIATION_MISMATCH"} & codes  # fmt: skip
    later = r.o.now() + timedelta(minutes=11)
    assert "RECONCILIATION_STALE" in r.o.r.gate(later).reason_codes


# ======================================== reconciliation and ambiguous orders


def test_reconciliation_supports_operator_absence_and_goes_stale(r: ReconEnv) -> None:
    r.o.sim.script_submit(FailBeforeReceipt())
    coid = placed(r)
    rec = r.o.recovery
    for minutes in (1, 4, 21):
        rec.cycle(now=r.o.clock.advance(timedelta(minutes=minutes)))
    rec.list_scan(coid, now=r.o.now())
    rec.activity_scan(coid, now=r.o.now())
    rep = r.run(timedelta(seconds=5))
    assert rep.outcome is Outcome.INCONCLUSIVE
    amb = [f for f in rep.findings if f.code == "AMBIGUOUS_ORDER_UNRESOLVED"]
    assert amb and amb[0].detail_dict()["absence_consistent"] == "True"
    stale = rec.confirm_absent(coid, operator="op", now=r.o.now() + timedelta(minutes=11))
    assert "no clean, fresh reconciliation run after the evidence was gathered" in stale.missing
    done = rec.confirm_absent(coid, operator="op", now=r.o.now() + timedelta(seconds=1))
    assert done.complete and r.o.order(coid).status is S.CONFIRMED_ABSENT
    assert r.o.reservation(coid).status is ReservationStatus.RELEASED
    assert len(r.o.submits(coid)) == 1


def test_absence_not_supported_when_anything_else_is_open(r: ReconEnv) -> None:
    r.o.sim.script_submit(FailBeforeReceipt())
    placed(r)
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "pending_reg_taf_fees": "0.01"})
    rep = r.run(QUIET)
    amb = [f for f in rep.findings if f.code == "AMBIGUOUS_ORDER_UNRESOLVED"]
    assert amb[0].detail_dict()["absence_consistent"] == "False"


# ================================================================ boundary


def test_reconciler_is_read_only_at_the_broker() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "oms"
    tree = ast.parse((src / "reconciler.py").read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}  # fmt: skip
    assert not {"submit_order", "cancel_order", "submit", "cancel"} & called
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "swing_trading_bot.oms.execution" not in imported
    assert not any("alpaca" in (m or "") for m in imported)
    for path in src.glob("*.py"):
        text = path.read_text()
        assert "os.environ" not in text and ".env" not in text.replace(".environment", "")


# ============================================== inconclusive escalation policy


def _inconclusive(r: ReconEnv, after: timedelta) -> Any:
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "pending_reg_taf_fees": "0.01"})
    rep = r.run(after)
    assert rep.outcome is Outcome.INCONCLUSIVE and rep.codes() == {"FEES_PENDING"}
    return rep


def test_immediate_retries_do_not_count_as_separate_cycles(r: ReconEnv) -> None:
    first = _inconclusive(r, QUIET)
    burst = [_inconclusive(r, timedelta(seconds=1)) for _ in range(8)]
    assert [x.halted for x in (first, *burst)] == [None] * 9
    assert {x.evidence["inconclusive_cycle"] for x in burst} == {1}
    gate = r.o.r.gate(r.o.now())
    assert "RECONCILIATION_INCONCLUSIVE" in gate.reason_codes  # entries stay blocked
    assert not gate.new_entries_allowed


def test_distinct_cycles_escalate_on_the_fifth(r: ReconEnv) -> None:
    reps = [_inconclusive(r, QUIET)]
    for _ in range(4):
        reps.append(_inconclusive(r, timedelta(seconds=30)))  # same cycle
        reps.append(_inconclusive(r, timedelta(seconds=30)))  # 60 s after the last counted
    cycles = [x.evidence["inconclusive_cycle"] for x in reps]
    assert cycles == [1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert [x.halted for x in reps] == [None] * 8 + [HaltReason.RECON_INCONCLUSIVE_REPEAT]


def test_clean_or_mismatch_run_resets_the_streak(r: ReconEnv) -> None:
    for _ in range(4):
        _inconclusive(r, MIN)
    assert r.run(MIN).outcome is Outcome.CLEAN  # the streak ends
    reps = [_inconclusive(r, MIN) for _ in range(4)]
    assert reps[-1].evidence["inconclusive_cycle"] == 4 and reps[-1].halted is None
    r.o.sim.corrupt_next("get_account", lambda raw: {**raw, "cash": "1"})
    assert r.run(MIN).outcome is Outcome.MISMATCH
    assert _inconclusive(r, MIN).evidence["inconclusive_cycle"] == 1
