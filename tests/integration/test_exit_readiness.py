"""Step 10 refinement: read-only exit readiness, separate from new-entry gates."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.broker.models import SubmitOrderRequest
from swing_trading_bot.broker.sim import SimConfig
from swing_trading_bot.domain.enums import (
    HaltReason,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.risk.exits import ExitFailure, ExitReadiness, ExitReadinessAssessor
from tests.support.gates import Env, healthy_env, record_identity
from tests.support.loss_limits import hold
from tests.support.portfolio import NOW, make_order, mark

D = Decimal


def assessor(env: Env, mode: RuntimeMode = RuntimeMode.SIMULATE) -> ExitReadinessAssessor:
    return ExitReadinessAssessor(env.db, env.pf, env.monitor, env.sim, mode=mode,
                                 trading_enabled=True)  # fmt: skip


def broker_buy(env: Env, qty: str, symbol: str = "AAPL") -> str:
    req = SubmitOrderRequest(new_client_order_id(), symbol, Side.BUY, D(qty), OrderType.LIMIT,
                             TimeInForce.DAY, D("100.00"))  # fmt: skip
    env.sim.submit_order(req)
    env.sim.schedule_fill(req.client_order_id, D(qty), D("100.00"))
    return req.client_order_id


def held_on_both(env: Env, qty: str = "10") -> None:
    broker_buy(env, qty)
    hold(env.db, env.pf, "AAPL", qty, "100.00", at=NOW)


def codes(r: ExitReadiness) -> set[str]:
    return {f.code for f in r.failures}


def test_confirmed_holding_is_safe_to_attempt(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    r = assessor(env).assess("AAPL", now=NOW)
    assert r.safe_to_attempt, r.failures
    assert (r.ledger_qty, r.broker_qty, r.confirmed_qty) == (D(10), D(10), D(10))
    assert r.execution_available is False


def test_halt_and_loss_limits_are_entry_only(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env, "20")
    with env.db.transaction() as tx:
        env.pf.record_mark(tx, mark("AAPL", "87.00", as_of=NOW, received=NOW))
    env.monitor.evaluate(now=NOW)  # daily lockout + weekly breach (+ persistent halt)
    with env.db.transaction() as tx:
        TradingStateRepository().halt(tx, HaltReason.OPERATOR, detail="x", actor="op", at=NOW)
    r = assessor(env).assess("AAPL", now=NOW)
    assert r.safe_to_attempt, r.failures  # a risk-reducing exit stays permissible
    assert {"DAILY_LOSS_LOCKOUT", "WEEKLY_LIMIT_ACTIVE", "PERSISTENT_HALT:OPERATOR"} <= set(
        r.entry_only_restrictions)  # fmt: skip


def test_missing_position_evidence_is_never_safe(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    broker_buy(env, "10")  # broker holds it, ledger does not
    r = assessor(env).assess("AAPL", now=NOW)
    assert ExitFailure.NO_LEDGER_POSITION in codes(r) and r.confirmed_qty is None
    env2 = healthy_env(tmp_path / "b")
    hold(env2.db, env2.pf, "AAPL", "10", "100.00", at=NOW)  # ledger holds it, broker does not
    assert ExitFailure.NO_BROKER_POSITION in codes(assessor(env2).assess("AAPL", now=NOW))


def test_quantity_disagreement(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    broker_buy(env, "7")
    hold(env.db, env.pf, "AAPL", "10", "100.00", at=NOW)
    r = assessor(env).assess("AAPL", now=NOW)
    assert ExitFailure.POSITION_MISMATCH in codes(r)
    assert (r.ledger_qty, r.broker_qty) == (D(10), D(7))


def test_conflicting_order_state(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    make_order(env.db, side=Side.SELL, qty="5", limit="105.00", status=OrderStatus.SUBMIT_AMBIGUOUS)
    r = assessor(env).assess("AAPL", now=NOW)
    assert ExitFailure.UNRESOLVED_LOCAL_ORDERS in codes(r)
    env2 = healthy_env(tmp_path / "b")
    held_on_both(env2)
    sell = SubmitOrderRequest(new_client_order_id(), "AAPL", Side.SELL, D(5), OrderType.LIMIT,
                              TimeInForce.DAY, D("120.00"))  # fmt: skip
    env2.sim.submit_order(sell)
    assert ExitFailure.BROKER_OPEN_ORDERS in codes(assessor(env2).assess("AAPL", now=NOW))
    env3 = healthy_env(tmp_path / "c")
    held_on_both(env3)
    make_order(env3.db, status=OrderStatus.ORPHAN)
    assert ExitFailure.UNRESOLVED_LOCAL_ORDERS in codes(assessor(env3).assess("AAPL", now=NOW))


def test_broker_connectivity_and_identity(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    env.sim.fail_next("get_positions", ServiceUnavailableError("503"))
    r = assessor(env).assess("AAPL", now=NOW)
    assert ExitFailure.BROKER_UNREACHABLE in codes(r) and not r.safe_to_attempt
    other = healthy_env(tmp_path / "b", sim_config=SimConfig(account_id="someone-else"))
    assert ExitFailure.BROKER_IDENTITY_MISMATCH in codes(assessor(other).assess("AAPL", now=NOW))
    record_identity(other.db, account_id="someone-else", environment="paper")
    found = codes(assessor(other).assess("AAPL", now=NOW))
    assert ExitFailure.BROKER_ENVIRONMENT_MISMATCH in found
    restricted = healthy_env(tmp_path / "c", sim_config=SimConfig(trading_blocked=True))
    assert ExitFailure.BROKER_ACCOUNT_RESTRICTED in codes(
        assessor(restricted).assess("AAPL", now=NOW))  # fmt: skip


def test_ledger_inconsistency_is_fundamental(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    with env.db.transaction() as tx:
        tx.execute("UPDATE positions SET cost_basis = '1' WHERE symbol = 'AAPL'")
    r = assessor(env).assess("AAPL", now=NOW + timedelta(seconds=1))
    assert ExitFailure.LEDGER_INVARIANT_VIOLATED in codes(r)


def test_assessment_is_read_only_and_deterministic(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    before = env.db.path.read_bytes()
    a, b = assessor(env).assess("AAPL", now=NOW), assessor(env).assess("AAPL", now=NOW)
    assert a == b
    assert env.db.path.read_bytes() == before
