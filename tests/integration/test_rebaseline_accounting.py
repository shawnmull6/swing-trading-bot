"""Step 13.2: the rebaseline cash equation and event-specific acknowledgements (§12.19)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.domain.enums import OrderType, Side, TimeInForce
from swing_trading_bot.oms.rebaseline import Decision, Rebaseliner, cumulative_acknowledged
from swing_trading_bot.oms.reconciler import Outcome, Severity, resume_after_clean_reconciliation
from tests.support.oms_crash import reopen
from tests.support.recon import ReconEnv, recon_env

D = Decimal
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
QUIET = timedelta(minutes=20)


@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def rb(r: ReconEnv) -> Rebaseliner:
    return Rebaseliner(r.o.db, r.o.r.env.pf, r.reconciler())


def activity(r: ReconEnv, kind: str, amount: str, offset: int = 5) -> None:
    r.o.sim.schedule_cash_activity(kind, D(amount), after=timedelta(seconds=offset))  # type: ignore[arg-type]


def silent(r: ReconEnv, amount: str) -> None:
    """Cash moves at the broker with no activity at all (e.g. a reset)."""
    r.o.sim._cash += D(amount)


def propose(r: ReconEnv) -> Any:
    p = rb(r).propose(actor="op", reason="review", now=r.o.now())
    assert p.decision is Decision.PROPOSED, p.reasons
    return p


def approve(r: ReconEnv, typed: str) -> Any:
    p = propose(r)
    c = rb(r).confirm(
        p.request_id, acknowledged=typed, actor="op", reason="ok", now=r.o.now() + SEC
    )
    assert c.decision is Decision.APPROVED, c.reasons
    return p


def items(p: Any) -> list[tuple[str, Decimal | None, bool]]:
    return sorted((a.kind, a.amount, a.in_baseline) for a in p.preview.activities)


# ===================================================== the cash equation


def _multiple(r: ReconEnv) -> None:
    activity(r, "CSD", "200.00", 5)
    activity(r, "CSW", "-50.00", 6)
    activity(r, "FEE", "-1.25", 7)


def _offsetting(r: ReconEnv) -> None:
    activity(r, "CSD", "100.00", 5)
    activity(r, "CSW", "-100.00", 6)


def _residual_plus_activity(r: ReconEnv) -> None:
    silent(r, "-100.00")
    activity(r, "CSD", "100.00")


CASES: dict[str, tuple[Any, str, list[str], str]] = {
    # a $200 deposit with no activity: all of it is residual
    "silent-deposit": (lambda r: silent(r, "200.00"), "200.00", [], "200.00"),
    # the same deposit reported as an activity: residual 0, one $200 item (not $400)
    "deposit-activity": (lambda r: activity(r, "CSD", "200.00"), "0", ["200.00"], "200.00"),
    "withdrawal": (lambda r: activity(r, "CSW", "-150.00"), "0", ["-150.00"], "-150.00"),
    "fee": (lambda r: activity(r, "FEE", "-1.25"), "0", ["-1.25"], "-1.25"),  # no order ref
    "multiple": (_multiple, "0", ["-1.25", "-50.00", "200.00"], "148.75"),
    # offsetting deposit and withdrawal: net 0, but both still need acknowledging
    "offsetting": (_offsetting, "0", ["-100.00", "100.00"], "0"),
    # a silent -$100 and a reported +$100: residual -100, one item, net 0
    "residual-plus-activity": (_residual_plus_activity, "-100.00", ["100.00"], "0"),
}


@pytest.mark.parametrize(
    ("arrange", "residual", "counted", "net"), list(CASES.values()), ids=list(CASES)
)
def test_cash_equation(r: ReconEnv, arrange: Any, residual: str, counted: list[str],
                       net: str) -> None:  # fmt: skip
    arrange(r)
    assert r.run(QUIET).outcome is Outcome.MISMATCH
    p = propose(r)
    assert p.preview.residual == D(residual)
    assert sorted(a.amount for a in p.preview.activities) == sorted(D(c) for c in counted)
    assert not any(a.in_baseline for a in p.preview.activities)
    assert p.preview.net == p.unexplained == D(net)
    c = rb(r).confirm(p.request_id, acknowledged=net, actor="op", reason="ok", now=r.o.now() + SEC)
    assert c.decision is Decision.APPROVED, c.reasons
    rep = r.run(MIN)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert r.o.balances().cash == D(5000)  # the virtual ledger never moves


def test_decimal_precision_and_exact_confirmation(r: ReconEnv) -> None:
    activity(r, "CSD", "0.10", 5)
    activity(r, "CSD", "0.20", 6)
    r.run(QUIET)
    p = propose(r)
    assert p.preview.net == D("0.30")
    for wrong in ("0.30000001", "0.29", "0.31", "3e-1x"):
        out = rb(r).confirm(p.request_id, acknowledged=wrong, actor="op", reason="x", now=r.o.now())
        assert out.decision is Decision.REFUSED
    ok = rb(r).confirm(
        p.request_id, acknowledged="0.3", actor="op", reason="x", now=r.o.now() + SEC
    )
    assert ok.decision is Decision.APPROVED  # exact Decimal value, any spelling


def test_zero_net_requires_explicit_zero(r: ReconEnv) -> None:
    activity(r, "CSD", "100.00", 5)
    activity(r, "CSW", "-100.00", 6)
    r.run(QUIET)
    p = propose(r)
    assert p.preview.net == 0 and len(p.preview.activities) == 2
    assert (
        rb(r)
        .confirm(p.request_id, acknowledged="0.01", actor="op", reason="x", now=r.o.now())
        .decision
        is Decision.REFUSED
    )
    assert (
        rb(r)
        .confirm(p.request_id, acknowledged="0.00", actor="op", reason="x", now=r.o.now() + SEC)
        .decision
        is Decision.APPROVED
    )


# ===================================================== event-specific acknowledgement


def test_acknowledged_manual_deposit_is_recorded_exactly(r: ReconEnv) -> None:
    activity(r, "CSD", "250.00")
    r.run(QUIET)
    p = approve(r, "250.00")
    with r.o.db.read() as s:
        acks = s.fetch_all("SELECT k.kind, k.ref_id, k.payload = a.raw_json, k.amount "
                           "FROM rebaseline_acknowledgements k JOIN broker_activities a "
                           "ON a.activity_id = k.ref_id")  # fmt: skip
        stored = s.fetch_one("SELECT attribution FROM broker_activities")
    assert [(k, ref, same, D(str(amt))) for k, ref, same, amt in acks] == [
        ("ACTIVITY", p.preview.activities[0].ref_id, 1, D(250))
    ]
    assert stored == ("UNEXPLAINED",)  # classification and payload are preserved
    assert r.run(MIN).outcome is Outcome.CLEAN
    assert "UNEXPLAINED_ACTIVITY" not in r.o.r.gate(r.o.now()).reason_codes


def test_unseen_pre_baseline_manual_trade_is_still_evaluated(r: ReconEnv) -> None:
    # A manual round trip executes now, but its FILL activities post hours later.
    for coid, side in (("manual-b", Side.BUY), ("manual-s", Side.SELL)):
        r.o.sim.submit_order(
            SubmitOrderRequest(coid, "AAPL", side, D(1), OrderType.LIMIT, TimeInForce.DAY, D(100))
        )
        r.o.sim.schedule_fill(coid, D(1), D(100), after=SEC, activity_delay=timedelta(hours=3))
        r.o.clock.advance(2 * SEC)  # the buy fills before the sell is placed
    activity(r, "CSD", "250.00", 10)
    r.run(QUIET)
    p = approve(r, "250.00")  # acknowledges the deposit and the two (closed) orders
    assert {o.ref_id for o in p.preview.orders} == {"manual-b", "manual-s"}
    assert r.run(MIN).outcome is Outcome.CLEAN
    r.o.clock.advance(timedelta(hours=3))  # the fills appear, timestamped before the baseline
    rep = r.run(MIN)
    assert rep.outcome is Outcome.MISMATCH
    assert [f.code for f in rep.findings if f.severity is Severity.MISMATCH].count(
        "UNEXPLAINED_FILL") == 2  # fmt: skip
    assert "UNEXPLAINED_ACTIVITY" in r.o.r.gate(r.o.now()).reason_codes
    second = propose(r)  # a second rebaseline would list ONLY the new events
    assert {a.ref_id for a in second.preview.activities} == {
        f.subject for f in rep.findings if f.code == "UNEXPLAINED_FILL"}  # fmt: skip
    assert second.preview.orders == ()


def test_replayed_acknowledged_events_stay_acknowledged(r: ReconEnv) -> None:
    activity(r, "CSD", "250.00")
    r.run(QUIET)
    approve(r, "250.00")
    for _ in range(3):  # every run re-reads the full history
        assert r.run(MIN).outcome is Outcome.CLEAN
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM broker_activities") == (1,)


def test_conflicting_payload_fails_closed(r: ReconEnv) -> None:
    activity(r, "CSD", "250.00")
    r.run(QUIET)
    approve(r, "250.00")
    r.o.sim.corrupt_next("get_activities", lambda raw: {**raw, "net_amount": "2500.00"})
    rep = r.run(MIN)
    assert rep.outcome is Outcome.MISMATCH and "ACTIVITY_CONFLICT" in rep.codes()
    with r.o.db.read() as s:
        stored = s.fetch_one("SELECT net_amount FROM broker_activities")
    assert stored is not None and D(str(stored[0])) == D(250)  # never rewritten


def test_acknowledgement_requires_the_identical_payload(r: ReconEnv) -> None:
    activity(r, "CSD", "250.00")
    r.run(QUIET)
    p = approve(r, "250.00")
    aid = p.preview.activities[0].ref_id
    activity(r, "CSD", "10.00", 30)
    r.run(MIN)  # the new deposit is unacknowledged
    with r.o.db.transaction() as tx:  # an acknowledgement row with a different payload ...
        new_id = tx.fetch_one(
            "SELECT activity_id FROM broker_activities WHERE activity_id <> ?", (aid,)
        )[0]  # type: ignore[index]
        tx.insert(
            "INSERT INTO rebaseline_acknowledgements (request_id, kind, ref_id, payload, "
            "amount, in_baseline) VALUES (?, 'ACTIVITY', ?, '{\"other\":1}', '10', 0)",
            (p.request_id, new_id),
        )
    rep = r.run(MIN)  # ... does not acknowledge the stored event
    assert any(f.subject == new_id for f in rep.findings if f.severity is Severity.MISMATCH)
    assert "UNEXPLAINED_ACTIVITY" in r.o.r.gate(r.o.now()).reason_codes


def test_multiple_independent_unexplained_activities(r: ReconEnv) -> None:
    activity(r, "CSD", "100.00", 5)
    activity(r, "CSD", "50.00", 6)
    r.run(QUIET)
    first = approve(r, "150.00")
    assert len(first.preview.activities) == 2
    assert r.run(MIN).outcome is Outcome.CLEAN
    activity(r, "CSW", "-30.00", 10)  # a third, independent event
    rep = r.run(MIN)
    flagged = {f.subject for f in rep.findings if f.code == "UNEXPLAINED_CASH_ACTIVITY"}
    assert len(flagged) == 1  # only the new one
    second = approve(r, "-30.00")
    assert {a.ref_id for a in second.preview.activities} == flagged
    with r.o.db.read() as s:
        assert cumulative_acknowledged(s) == D(120)
    assert r.run(MIN).outcome is Outcome.CLEAN


def test_restart_after_rebaseline(r: ReconEnv) -> None:
    activity(r, "CSD", "250.00")
    r.run(QUIET)
    approve(r, "250.00")
    r.o.db.close()
    restarted = ReconEnv(reopen(r.o))
    rep = restarted.run(MIN)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert restarted.o.state().reason_code == "REBASELINE"  # the halt survived
    assert resume_after_clean_reconciliation(restarted.o.db, actor="op", detail="ok",
                                             now=restarted.o.now(),
                                             max_age=timedelta(minutes=10))  # fmt: skip
    assert not restarted.o.state().halted


def test_dividend_can_be_acknowledged_but_is_never_booked(r: ReconEnv) -> None:
    r.o.sim.schedule_cash_activity(
        "DIV",
        D("3.00"),
        symbol="XYZ",
        qty=D(10),
        per_share_amount=D("0.30"),
        after=timedelta(seconds=5),
    )
    assert "DIVIDEND_ENTITLEMENT_UNVERIFIED" in r.run(QUIET).codes()
    approve(r, "3.00")
    assert r.run(MIN).outcome is Outcome.CLEAN
    assert r.o.balances().cash == D(5000)
