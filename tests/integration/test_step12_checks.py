"""Step 12 final safety checks (requested with the step 12 approval).

1. Durable send marker: every broker write requires a previously COMMITTED marker;
   crashes just before and just after the marker is committed; never a resubmission.
2. Adoption with unbooked fills: reservation retained, new risk blocked, no cash or
   settlement inferred until the late activities are booked.
3. Execution boundary: an explicit simulation-only capability; registered test doubles
   only; arbitrary implementations and the Alpaca adapter refused.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import AcceptThenFail, SimBroker
from swing_trading_bot.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from swing_trading_bot.ledger.repositories import OrdersRepository, ReservationStatus
from swing_trading_bot.oms.execution import (
    CommittedSendMarker,
    ExecutionNotPermittedError,
    MarkerMissingError,
    SimulationExecution,
    read_committed_cancel_marker,
    read_committed_send_marker,
    register_simulation_double,
)
from swing_trading_bot.oms.recovery import recover_after_restart
from swing_trading_bot.risk.gates import GateReason
from tests.support.oms import OmsEnv, SimulatedCrash, crash_at, oms_env
from tests.support.oms_crash import CRASH_EXIT_CODE, reopen, run_child

D = Decimal
S = OrderStatus
MIN = timedelta(minutes=1)
SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"


def only_order(e: OmsEnv) -> str:
    with e.db.read() as s:
        rows = s.fetch_all("SELECT client_order_id FROM orders")
    assert len(rows) == 1
    return str(rows[0][0])


def restart(e: OmsEnv, after: timedelta = MIN) -> None:
    e.rebuild()
    e.advance(after)
    recover_after_restart(e.recovery, e.sync, now=e.now())


# ================================================== 1. durable send marker


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]  # fmt: skip


def test_every_execution_path_requires_a_committed_marker() -> None:
    """Static proof over src/: the broker write methods are called in exactly one place
    each (the capability), and the capability's entry points are called only right after
    reading the committed marker in the same function."""
    raw_writes: dict[str, list[str]] = {"submit_order": [], "cancel_order": []}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for attr, where in raw_writes.items():
            if _calls(tree, attr):
                where.append(path.relative_to(SRC).as_posix())
    assert raw_writes == {"submit_order": ["oms/execution.py"],
                          "cancel_order": ["oms/execution.py"]}  # fmt: skip

    om = ast.parse((SRC / "oms" / "order_manager.py").read_text())
    for func in (n for n in ast.walk(om) if isinstance(n, ast.FunctionDef)):
        for capability_call, proof in (("submit", "read_committed_send_marker"),
                                       ("cancel", "read_committed_cancel_marker")):  # fmt: skip
            calls = [c for c in _calls(func, capability_call)
                     if any(k.arg == "marker" for k in c.keywords)]  # fmt: skip
            if not calls:
                continue
            proofs = [n for n in ast.walk(func) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name) and n.func.id == proof]  # fmt: skip
            assert proofs, f"{func.name} calls .{capability_call}() without {proof}"
            assert all(p.lineno < c.lineno for p in proofs for c in calls)


def test_markers_cannot_be_forged_or_reused(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_intent_commit"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    with pytest.raises(MarkerMissingError):  # no committed marker yet
        read_committed_send_marker(e.db, coid)
    with pytest.raises(MarkerMissingError):  # cannot be constructed by hand
        CommittedSendMarker(coid, e.now(), object())
    with pytest.raises(MarkerMissingError):
        read_committed_cancel_marker(e.db, coid)
    other = SubmitOrderRequest("stb-other", "MSFT", Side.BUY, D(1), OrderType.LIMIT,
                               TimeInForce.DAY, D(50))  # fmt: skip
    # A marker for one order can never authorize another order's submission.
    with e.db.transaction() as tx:
        OrdersRepository().record_send_attempt(tx, coid, e.now())
    marker = read_committed_send_marker(e.db, coid)
    with pytest.raises(MarkerMissingError):
        SimulationExecution(e.sim).submit(other, marker=marker)
    assert e.submits() == []


def test_crash_inside_the_marker_transaction(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("send_marker_uncommitted"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    assert e.order(coid).send_attempted_at is None  # rolled back with the dying tx
    assert e.submits() == []
    for _ in range(3):
        restart(e, timedelta(minutes=5))
    assert e.order(coid).status is S.SUBMIT_AMBIGUOUS  # still only via the protocol
    assert e.submits() == []  # never sent, never resubmitted
    assert e.reservation(coid).status is ReservationStatus.ACTIVE


def test_crash_right_after_the_marker_commit(tmp_path: Path) -> None:
    e = oms_env(tmp_path, crash=crash_at("after_send_marker"))
    with pytest.raises(SimulatedCrash):
        e.place()
    coid = only_order(e)
    assert e.order(coid).send_attempted_at is not None and e.submits() == []
    for _ in range(3):
        restart(e, timedelta(minutes=5))
    assert e.submits() == [] and e.order(coid).status is S.SUBMIT_AMBIGUOUS


MARKER_POINTS = [("send_marker_uncommitted", False), ("after_send_marker", True)]


@pytest.mark.parametrize(("point", "marker_set"), MARKER_POINTS)
def test_process_crash_around_marker_persistence(tmp_path: Path, point: str,
                                                 marker_set: bool) -> None:  # fmt: skip
    e = oms_env(tmp_path)
    e.db.close()
    result = run_child(e.sim, e.clock, e.r.env.db.path, "place", point)
    assert result.exitcode == CRASH_EXIT_CODE, result
    assert "submit_order" not in result.calls  # the child never reached the broker write
    e2 = reopen(e)
    coid = only_order(e2)
    assert (e2.order(coid).send_attempted_at is not None) is marker_set
    restart(e2)
    assert e2.submits() == [] and e2.order(coid).status is S.SUBMIT_AMBIGUOUS


# ============================================ 2. adoption with unbooked fills


def _blocked(e: OmsEnv) -> bool:
    gate = e.r.gate(e.now())
    e.r.set_marks({"AAPL": "100.00"}, at=e.now())
    other = e.decide("AAPL", "100.00", "99.00")
    return GateReason.UNBOOKED_FILLS.value in gate.reason_codes and (
        "UNBOOKED_FILLS" in other.rejection_codes
    )


@pytest.mark.parametrize("qty", ["5", "12"])
def test_late_fill_activities_keep_reservation_and_block_risk(tmp_path: Path, qty: str) -> None:
    e = oms_env(tmp_path)
    coid = e.place().client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(qty), D("49.50"), after=timedelta(seconds=5),
                        activity_delay=timedelta(minutes=40))  # fmt: skip
    e.advance(MIN)
    e.sync.sync(now=e.now())
    o = e.order(coid)
    assert o.broker_filled_qty == D(qty) and o.filled_qty == 0 and o.unbooked_fill_qty == D(qty)
    assert o.status in (S.ACCEPTED, S.PARTIALLY_FILLED)  # never terminal while unbooked
    res = e.reservation(coid)
    assert res.status is ReservationStatus.ACTIVE and res.remaining == D(600)
    assert res.remaining >= o.unbooked_fill_qty * D("50.00")  # covers the unbooked cost
    b = e.balances()
    assert b.cash == D(5000) and b.cost_basis_exposure == 0  # nothing inferred
    assert b.unsettled_proceeds == 0
    assert _blocked(e)
    e.advance(timedelta(minutes=40))
    e.sync.sync(now=e.now())  # the late activities arrive and are booked
    o = e.order(coid)
    assert o.filled_qty == D(qty) and o.unbooked_fill_qty == 0
    assert e.balances().cash == D(5000) - D(qty) * D("49.50")
    if qty == "12":
        assert o.status is S.FILLED
        assert e.reservation(coid).status is ReservationStatus.CONSUMED
    else:
        assert o.status is S.PARTIALLY_FILLED
        assert e.reservation(coid).remaining == D(600) - D(5) * D(50)
    assert GateReason.UNBOOKED_FILLS.value not in e.r.gate(e.now()).reason_codes


def test_adopted_ambiguous_order_with_unbooked_fills(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    e.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = e.place().client_order_id
    assert coid is not None
    e.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5),
                        activity_delay=timedelta(minutes=30))  # fmt: skip
    e.advance(MIN)
    e.recovery.cycle(now=e.now())  # adopted by lookup; the fill activity is not posted
    o = e.order(coid)
    assert o.status is S.ACCEPTED and o.broker_filled_qty == D(12) and o.filled_qty == 0
    assert e.reservation(coid).status is ReservationStatus.ACTIVE
    assert _blocked(e)
    e.advance(timedelta(minutes=31))
    e.sync.sync(now=e.now())
    assert e.order(coid).status is S.FILLED
    assert GateReason.UNBOOKED_FILLS.value not in e.r.gate(e.now()).reason_codes


# ================================================== 3. execution boundary


class Arbitrary:
    def submit_order(self, request: Any) -> Any: ...
    def cancel_order(self, broker_order_id: str) -> None: ...


class LooksLikeSim(SimBroker):
    """Even a SimBroker subclass is not accepted unless explicitly registered."""


def test_only_simbroker_and_registered_doubles_are_permitted(tmp_path: Path) -> None:
    e = oms_env(tmp_path)
    SimulationExecution(e.sim)  # the real simulator
    for target in (Arbitrary(), LooksLikeSim(e.clock)):
        with pytest.raises(ExecutionNotPermittedError):
            SimulationExecution(target)


def test_double_registration_is_explicit_and_guarded() -> None:
    class NoPurpose:
        def submit_order(self, request: Any) -> Any: ...
        def cancel_order(self, broker_order_id: str) -> None: ...

    with pytest.raises(ExecutionNotPermittedError, match="PURPOSE"):
        register_simulation_double(NoPurpose)

    class AlpacaLike(NoPurpose):
        SIMULATION_DOUBLE_PURPOSE = "pretend"

    with pytest.raises(ExecutionNotPermittedError, match="Alpaca"):
        register_simulation_double(AlpacaLike)

    class ReadOnly:
        SIMULATION_DOUBLE_PURPOSE = "no writes"

    with pytest.raises(ExecutionNotPermittedError, match="execution port"):
        register_simulation_double(ReadOnly)


def test_alpaca_adapter_remains_structurally_read_only() -> None:
    from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter  # noqa: PLC0415

    assert not hasattr(AlpacaPaperReadAdapter, "submit_order")
    assert not hasattr(AlpacaPaperReadAdapter, "cancel_order")
    with pytest.raises(ExecutionNotPermittedError):
        register_simulation_double(AlpacaPaperReadAdapter)
