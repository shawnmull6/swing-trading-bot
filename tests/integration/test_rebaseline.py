"""Step 13.1: the audited, two-phase rebaseline workflow (ARCHITECTURE §8.5, §12.18)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import IncompleteRetrievalError
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.domain.enums import (
    HaltReason,
    OrderType,
    ReconciliationResult,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.baselines import BrokerBaselinesRepository
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.repositories import (
    ReconciliationRunsRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.oms.rebaseline import Decision, Rebaseliner, cumulative_acknowledged
from swing_trading_bot.oms.reconciler import Outcome, resume_after_clean_reconciliation
from tests.support.oms import SimulatedCrash, crash_at
from tests.support.recon import ReconEnv, recon_env

D = Decimal
MIN = timedelta(minutes=1)
QUIET = timedelta(minutes=20)
BOOKS = ("cash_entries", "fills", "positions", "orders", "cash_reservations")


@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def rb(r: ReconEnv, crash: Any = None) -> Rebaseliner:
    return Rebaseliner(r.o.db, r.o.r.env.pf, r.reconciler(), crash=crash)


def deposit(r: ReconEnv, amount: str = "250.00") -> None:
    """A manual deposit into the paper account: confirmed, but not the bot's."""
    r.o.sim.schedule_cash_activity("CSD", D(amount), after=timedelta(seconds=5))
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.MISMATCH and rep.halted is HaltReason.RECON_CASH


def books(r: ReconEnv) -> str:
    with r.o.db.read() as s:
        return ledger_fingerprint(s, BOOKS)


def events(r: ReconEnv) -> list[str]:
    with r.o.db.read() as s:
        return [str(x[0]) for x in s.fetch_all("SELECT action FROM rebaseline_events ORDER BY id")]


def status(r: ReconEnv, request_id: int) -> str:
    with r.o.db.read() as s:
        row = s.fetch_one("SELECT status FROM rebaseline_requests WHERE id = ?", (request_id,))
    assert row is not None
    return str(row[0])


# ================================================================ happy paths


def test_full_workflow_after_a_manual_deposit(r: ReconEnv) -> None:
    deposit(r)
    before = books(r)
    p = rb(r).propose(actor="alice", reason="manual deposit by owner", now=r.o.now())
    assert p.decision is Decision.PROPOSED and p.unexplained == D(250)
    assert p.request_id is not None
    c = rb(r).confirm(
        p.request_id,
        acknowledged="250.00",
        actor="alice",
        reason="confirmed with statement",
        now=r.o.now() + MIN,
    )
    assert c.decision is Decision.APPROVED and c.new_baseline_id is not None
    # Nothing in the virtual books changed; the halt remains (now REBASELINE).
    assert books(r) == before and r.o.balances().cash == D(5000)
    state = r.o.state()
    assert state.halted and state.reason_code == HaltReason.REBASELINE
    assert not r.o.r.gate(r.o.now()).new_entries_allowed
    with r.o.db.read() as s:
        history = BrokerBaselinesRepository().history(s, 1)
        active = BrokerBaselinesRepository().active(s, 1)
        assert cumulative_acknowledged(s) == D(250)
    assert [(h[1], h[2] is not None) for h in history] == [("INIT", True), ("REBASELINE", False)]
    assert active is not None and active.cash == D(100250) and active.id == c.new_baseline_id
    assert events(r) == ["PROPOSED", "APPROVED"]
    # Resume needs a later CLEAN reconciliation against the new baseline.
    max_age = timedelta(minutes=10)
    assert not resume_after_clean_reconciliation(
        r.o.db, actor="alice", detail="too early", now=r.o.now() + MIN, max_age=max_age
    )
    rep = r.run(2 * MIN)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert resume_after_clean_reconciliation(
        r.o.db, actor="alice", detail="clean", now=r.o.now(), max_age=max_age
    )
    assert not r.o.state().halted


def test_silent_cash_change_such_as_an_account_reset(r: ReconEnv) -> None:
    r.o.sim._cash -= D("1234.56")
    rep = r.run(QUIET)
    assert "CASH_UNEXPLAINED" in rep.codes() and rep.halted is HaltReason.RECON_CASH
    p = rb(r).propose(actor="op", reason="paper account reset", now=r.o.now())
    assert p.decision is Decision.PROPOSED and p.unexplained == D("-1234.56")
    assert p.request_id is not None
    c = rb(r).confirm(
        p.request_id, acknowledged="-1234.56", actor="op", reason="reset", now=r.o.now() + MIN
    )
    assert c.decision is Decision.APPROVED
    assert r.run(2 * MIN).outcome is Outcome.CLEAN


# ================================================================ refusals


def test_propose_requires_a_persistent_halt(r: ReconEnv) -> None:
    p = rb(r).propose(actor="op", reason="why", now=r.o.now())
    assert p.decision is Decision.REFUSED and "not persistently halted" in p.reasons[0]
    assert events(r) == ["PROPOSE_REFUSED"]
    with r.o.db.read() as s:
        assert s.fetch_one("SELECT count(*) FROM rebaseline_requests") == (0,)


def test_explicit_operator_and_reason_required(r: ReconEnv) -> None:
    with pytest.raises(ValueError, match="actor"):
        rb(r).propose(actor=" ", reason="x", now=r.o.now())
    with pytest.raises(ValueError, match="reason"):
        rb(r).propose(actor="op", reason="", now=r.o.now())


def _refused(r: ReconEnv, fragment: str) -> None:
    p = rb(r).propose(actor="op", reason="attempt", now=r.o.now())
    assert p.decision is Decision.REFUSED
    assert any(fragment in why for why in p.reasons), p.reasons
    assert events(r)[-1] == "PROPOSE_REFUSED"


def test_refused_when_positions_differ(r: ReconEnv) -> None:
    r.o.sim.submit_order(
        SubmitOrderRequest(
            "manual-1", "AAPL", Side.BUY, D(2), OrderType.LIMIT, TimeInForce.DAY, D(100)
        )
    )
    r.o.sim.schedule_fill("manual-1", D(2), D(100), after=timedelta(seconds=5))
    r.run(QUIET)
    _refused(r, "positions differ")


def test_refused_with_working_orders(r: ReconEnv) -> None:
    r.o.advance(timedelta(0))
    assert r.o.place().client_order_id is not None  # an accepted, working bot order
    deposit(r)
    _refused(r, "open orders exist at the broker")


def test_refused_on_incomplete_evidence(r: ReconEnv) -> None:
    deposit(r)
    r.o.sim.fail_next("get_orders", IncompleteRetrievalError("saturated"))
    _refused(r, "evidence incomplete")


def test_refused_for_mismatches_it_cannot_acknowledge(r: ReconEnv) -> None:
    r.o.sim.submit_order(
        SubmitOrderRequest(
            new_client_order_id(), "MSFT", Side.BUY, D(1), OrderType.LIMIT, TimeInForce.DAY, D(50)
        )
    )
    r.run(QUIET)
    _refused(r, "cannot acknowledge")


def test_refused_when_there_is_nothing_to_acknowledge(r: ReconEnv) -> None:
    with r.o.db.transaction() as tx:
        TradingStateRepository().halt(
            tx, HaltReason.OPERATOR, detail="manual", actor="op", at=r.o.now()
        )
    r.o.clock.advance(QUIET)
    _refused(r, "nothing to acknowledge")


# ================================================================ confirmation


def _proposed(r: ReconEnv) -> int:
    deposit(r)
    p = rb(r).propose(actor="op", reason="deposit", now=r.o.now())
    assert p.request_id is not None
    return p.request_id


def test_wrong_typed_amount_is_refused_and_can_be_corrected(r: ReconEnv) -> None:
    rid = _proposed(r)
    for typed in ("250.01", "-250", "", "abc", "2.5e2x"):
        out = rb(r).confirm(rid, acknowledged=typed, actor="op", reason="x", now=r.o.now())
        assert out.decision is Decision.REFUSED and status(r, rid) == "PROPOSED"
    ok = rb(r).confirm(rid, acknowledged="250", actor="op", reason="x", now=r.o.now() + MIN)
    assert ok.decision is Decision.APPROVED  # exact value, any decimal spelling
    assert events(r).count("CONFIRM_REFUSED") == 5


def test_stale_preview_expires(r: ReconEnv) -> None:
    rid = _proposed(r)
    out = rb(r).confirm(
        rid, acknowledged="250.00", actor="op", reason="late", now=r.o.now() + timedelta(minutes=16)
    )
    assert out.decision is Decision.EXPIRED and status(r, rid) == "EXPIRED"
    again = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="late", now=r.o.now())
    assert again.decision is Decision.REFUSED and "EXPIRED" in again.reasons[0]


def test_changed_broker_evidence_expires(r: ReconEnv) -> None:
    rid = _proposed(r)
    r.o.sim._cash -= D("0.01")
    out = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert out.decision is Decision.EXPIRED
    assert "broker evidence changed since the preview" in out.reasons


def test_changed_ledger_state_expires(r: ReconEnv) -> None:
    rid = _proposed(r)
    with r.o.db.transaction() as tx:  # an audited adjustment recorded after the preview
        tx.insert(
            "INSERT INTO ledger_adjustments (ts, actor, kind, cash_delta, reason, evidence)"
            " VALUES ('2026-11-09T15:30:00.000000+00:00', 'op', 'CASH', '0', 'r', 'e')"
        )
    out = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert out.decision is Decision.EXPIRED
    assert "ledger state changed since the preview" in out.reasons


def test_confirm_requires_the_halt_to_still_be_active(r: ReconEnv) -> None:
    rid = _proposed(r)
    with r.o.db.transaction() as tx:
        run_id = ReconciliationRunsRepository().append(tx, ReconciliationResult.OK, None, r.o.now())
        TradingStateRepository().resume(
            tx, detail="x", actor="op", at=r.o.now(), reconciliation_run_id=run_id
        )
    out = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert out.decision is Decision.EXPIRED and "not persistently halted" in out.reasons[0]


def test_duplicate_confirmation_is_idempotent(r: ReconEnv) -> None:
    rid = _proposed(r)
    first = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now())
    second = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert first.decision is Decision.APPROVED and second.decision is Decision.DUPLICATE
    assert second.new_baseline_id == first.new_baseline_id
    other = rb(r).confirm(rid, acknowledged="1", actor="op", reason="x", now=r.o.now())
    assert other.decision is Decision.REFUSED
    with r.o.db.read() as s:
        assert len(BrokerBaselinesRepository().history(s, 1)) == 2  # exactly one new baseline
    assert events(r) == ["PROPOSED", "APPROVED", "DUPLICATE_CONFIRM", "CONFIRM_REFUSED"]


def test_preview_is_immutable(r: ReconEnv) -> None:
    rid = _proposed(r)
    with pytest.raises(PersistenceError), r.o.db.transaction() as tx:
        tx.execute("UPDATE rebaseline_requests SET unexplained_cash = '1' WHERE id = ?", (rid,))
    with pytest.raises(PersistenceError), r.o.db.transaction() as tx:
        tx.execute("DELETE FROM rebaseline_events")


# ================================================================ crash safety


def test_crash_inside_the_approval_transaction(r: ReconEnv) -> None:
    rid = _proposed(r)
    with pytest.raises(SimulatedCrash):
        rb(r, crash_at("rebaseline:before_commit")).confirm(
            rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now()
        )
    assert status(r, rid) == "PROPOSED"  # nothing of the approval landed
    with r.o.db.read() as s:
        assert len(BrokerBaselinesRepository().history(s, 1)) == 1
    out = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert out.decision is Decision.APPROVED


def test_crash_after_commit_then_retry_is_a_duplicate(r: ReconEnv) -> None:
    rid = _proposed(r)
    with pytest.raises(SimulatedCrash):
        rb(r, crash_at("rebaseline:after_commit")).confirm(
            rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now()
        )
    assert status(r, rid) == "APPROVED"
    out = rb(r).confirm(rid, acknowledged="250.00", actor="op", reason="x", now=r.o.now() + MIN)
    assert out.decision is Decision.DUPLICATE
