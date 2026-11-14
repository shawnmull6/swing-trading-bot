"""Step 10: startup readiness, transient blocks, persistent halts and gate reporting."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import AuthenticationError, ServiceUnavailableError
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from swing_trading_bot.domain.enums import (
    HaltReason,
    OrderStatus,
    ReconciliationResult,
    RuntimeMode,
)
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import ResumeOutcome, TradingStateRepository
from swing_trading_bot.risk.gates import GateLayer, GateReason, TradingGates
from tests.support.alpaca_fake import ACCOUNT, CLOCK, CONFIG, FakeTransport, js
from tests.support.calendar_fixture import trading_fetch
from tests.support.gates import GATE_CONFIG, Env, healthy_env, record_identity, resume
from tests.support.loss_limits import CONFIG as LOSS_CONFIG
from tests.support.loss_limits import close, et, hold, verified
from tests.support.portfolio import CONFIG as PF_CONFIG
from tests.support.portfolio import NOW, fill, make_order, mark, new_ledger, seed_calendar

D = Decimal
R = GateReason
MON, TUE, WED = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)


def codes(env: Env, at=None) -> set[str]:  # type: ignore[no-untyped-def]
    return set(env.gates.evaluate(now=at or env.clock.now()).reason_codes)


# ----------------------------------------------------------------- startup


def test_healthy_startup_allows_new_entries(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    before = env.gates.evaluate(now=NOW)
    assert before.reason_codes == ("STARTUP_INCOMPLETE",)
    assert not before.new_entries_allowed  # default: blocked until startup passes
    status = env.gates.run_startup(now=NOW)
    assert env.gates.startup.complete and status.startup_complete
    assert status.new_entries_allowed, status.render()
    assert status.findings == ()
    assert status.exits.permitted_in_principle and not status.exits.execution_available


def test_missing_prerequisites_keep_startup_incomplete(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, identity=False)
    status = env.gates.run_startup(now=NOW)
    assert not env.gates.startup.complete
    assert {"STARTUP_INCOMPLETE", "BROKER_IDENTITY_UNRECORDED"} <= set(status.reason_codes)


def test_uninitialized_ledger_and_missing_reconciliation(tmp_path: Path) -> None:
    db = Database.create(tmp_path / "empty.sqlite3", RuntimeMode.SIMULATE)
    pf = VirtualPortfolio(db, PF_CONFIG)
    clock = SimClock(NOW)
    sim = SimBroker(clock)
    sim.load_calendar(trading_fetch(NOW))
    gates = TradingGates(db, pf, LossLimitMonitor(db, pf, LOSS_CONFIG), sim, GATE_CONFIG)
    status = gates.run_startup(now=NOW)
    assert {"PORTFOLIO_MISSING", "PERSISTENT_HALT", "STARTUP_INCOMPLETE"} <= set(
        status.reason_codes
    )
    assert not status.exits.permitted_in_principle
    db.close()
    env = healthy_env(tmp_path / "x", reconcile=False)
    status = env.gates.run_startup(now=NOW)
    assert {"RECONCILIATION_REQUIRED", "PERSISTENT_HALT"} <= set(status.reason_codes)
    assert not env.gates.startup.complete


def test_wrong_broker_environment_and_identity(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(account_id="someone-else"))
    status = env.gates.run_startup(now=NOW)
    assert "BROKER_IDENTITY_MISMATCH" in status.reason_codes and not env.gates.startup.complete
    assert not status.exits.permitted_in_principle  # even exits would hit the wrong account
    env2 = healthy_env(tmp_path / "b")
    record_identity(env2.db, environment="paper")  # ledger says Alpaca paper, broker is sim
    assert "BROKER_ENVIRONMENT_MISMATCH" in env2.gates.run_startup(now=NOW).reason_codes
    observe = replace(GATE_CONFIG, mode=RuntimeMode.OBSERVE)
    gates = TradingGates(env2.db, env2.pf, env2.monitor, env2.sim, observe)
    found = set(gates.run_startup(now=NOW).reason_codes)
    assert {
        "LEDGER_MODE_MISMATCH",
        "BROKER_ENVIRONMENT_MISMATCH",
        "RUNTIME_MODE_NO_EXECUTION",
    } <= found


def test_unsupported_account_configuration(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(suspend_trade=True))
    assert "BROKER_ACCOUNT_CONFIG_UNSUPPORTED" in env.gates.run_startup(now=NOW).reason_codes
    lax = healthy_env(
        tmp_path / "b", sim_config=SimConfig(no_shorting=False, max_margin_multiplier=D(4))
    )
    status = lax.gates.run_startup(now=NOW)
    assert status.new_entries_allowed  # U6: report-only in M1
    assert [f.reason for f in status.advisories] == [R.ACCOUNT_PROTECTIONS_NOT_SET]
    strict = TradingGates(
        lax.db,
        lax.pf,
        lax.monitor,
        lax.sim,
        replace(GATE_CONFIG, require_m2_account_protections=True),
    )
    assert "ACCOUNT_PROTECTIONS_NOT_SET" in strict.run_startup(now=NOW).reason_codes


# --------------------------------------------------------- transient blocks


def test_multiple_transient_blocks_are_all_reported_deterministically(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env.gates.run_startup(now=NOW)
    gates = TradingGates(
        env.db,
        env.pf,
        env.monitor,
        env.sim,
        replace(GATE_CONFIG, trading_enabled=False),
        startup=env.gates.startup,
    )
    order = make_order(env.db, status=OrderStatus.SUBMIT_AMBIGUOUS)
    later = NOW + timedelta(minutes=5)
    env.clock.advance_to(later)  # sync and recon now 5 minutes old
    first = gates.evaluate(now=later)
    second = gates.evaluate(now=later)
    assert first == second and first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict(), sort_keys=True)  # serialisable
    assert {"TRADING_DISABLED", "BROKER_SYNC_STALE", "UNRESOLVED_ORDERS"} <= set(first.reason_codes)
    assert list(first.findings) == sorted(first.findings)
    unresolved = next(f for f in first.findings if f.reason is R.UNRESOLVED_ORDERS)
    assert order.client_order_id in unresolved.detail
    assert "STARTUP_INCOMPLETE" not in first.reason_codes


def test_startup_never_overrides_transient_blocks(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    gates = TradingGates(
        env.db, env.pf, env.monitor, env.sim, replace(GATE_CONFIG, trading_enabled=False)
    )
    status = gates.run_startup(now=NOW)
    assert gates.startup.complete
    assert status.reason_codes == ("TRADING_DISABLED",)


def test_market_closed(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env.gates.run_startup(now=NOW)
    evening = et(MON, 17)
    env.refresh(evening)
    env.monitor.establish_baselines(now=evening)
    assert "MARKET_CLOSED" in codes(env, evening)


def test_stale_or_missing_prices(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    hold(env.db, env.pf, "AAPL", "10", "100.00", at=NOW)
    env.gates.run_startup(now=NOW)
    assert {"MARKS_STALE_OR_MISSING", "EQUITY_UNKNOWN"} <= codes(env, NOW)
    with env.db.transaction() as tx:
        env.pf.record_mark(
            tx, mark("AAPL", "100.00", as_of=NOW - timedelta(minutes=1), received=NOW)
        )
    assert "MARKS_STALE_OR_MISSING" not in codes(env, NOW)
    later = NOW + timedelta(minutes=20)
    env.refresh(later)
    assert "MARKS_STALE_OR_MISSING" in codes(env, later)


def test_calendar_unavailable_or_incomplete(tmp_path: Path) -> None:
    db, pf = new_ledger(tmp_path / "nocal.sqlite3", with_calendar=False)
    clock = SimClock(NOW)
    sim = SimBroker(clock)
    sim.load_calendar(trading_fetch(NOW))
    record_identity(db)
    gates = TradingGates(db, pf, LossLimitMonitor(db, pf, LOSS_CONFIG), sim, GATE_CONFIG)
    assert "CALENDAR_UNAVAILABLE" in gates.evaluate(now=NOW).reason_codes
    seed_calendar(db, NOW, date(2025, 12, 1), date(2026, 11, 13))
    found = set(gates.evaluate(now=NOW).reason_codes)
    assert "CALENDAR_COVERAGE_INSUFFICIENT" in found
    assert "CALENDAR_HORIZON_SHORT" in {f.reason.value for f in gates.evaluate(now=NOW).advisories}
    db.close()


def test_unresolved_orders_and_unexplained_activity(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env.gates.run_startup(now=NOW)
    make_order(env.db, status=OrderStatus.PENDING_SUBMIT)
    env.pf.process_fill(
        fill(None, "1", "10.00", client_order_id="manual-1", activity_id="ext-1"), now=NOW
    )
    found = codes(env, NOW)
    assert {"UNRESOLVED_ORDERS", "UNEXPLAINED_ACTIVITY", "PERSISTENT_HALT"} <= found


def test_incomplete_activity_collection(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env.gates.run_startup(now=NOW)
    later = NOW + timedelta(minutes=30)
    env.refresh(later, coverage=False)  # windows since then were never complete
    status = env.gates.evaluate(now=later)
    finding = next(f for f in status.findings if f.reason is R.ACTIVITY_COVERAGE_INCOMPLETE)
    assert "complete through" in finding.detail


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ServiceUnavailableError("503"), "BROKER_UNAVAILABLE"),
        (AuthenticationError("401", status_code=401), "BROKER_AUTH_FAILED"),
    ],
)
def test_simbroker_read_failures_block_and_make_exits_unsafe(
    tmp_path: Path, error: Exception, reason: str
) -> None:
    env = healthy_env(tmp_path)
    env.gates.run_startup(now=NOW)
    env.sim.fail_next("get_account", error)  # type: ignore[arg-type]
    status = env.gates.evaluate(now=NOW)
    assert reason in status.reason_codes
    assert not status.exits.permitted_in_principle
    env.sim.corrupt_next("get_clock", lambda r: {**r, "is_open": "yes"})
    assert "BROKER_DATA_INVALID" in env.gates.evaluate(now=NOW).reason_codes


def test_restricted_broker_account(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(trading_blocked=True))
    assert "BROKER_ACCOUNT_RESTRICTED" in env.gates.run_startup(now=NOW).reason_codes


# --------------------------------------------------------- persistent halts


def test_persistent_halt_survives_restart_and_startup_cannot_override(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    with env.db.transaction() as tx:
        TradingStateRepository().halt(
            tx, HaltReason.OPERATOR, detail="maintenance", actor="operator", at=NOW
        )
    path = env.db.path
    env.db.close()
    db = Database.open(path, RuntimeMode.SIMULATE)
    pf = VirtualPortfolio(db, PF_CONFIG)
    gates = TradingGates(db, pf, LossLimitMonitor(db, pf, LOSS_CONFIG), env.sim, GATE_CONFIG)
    status = gates.run_startup(now=NOW)
    assert gates.startup.complete  # every prerequisite passed ...
    assert status.reason_codes == ("PERSISTENT_HALT",)  # ... yet the halt still blocks
    assert status.persistent_halt and "OPERATOR: maintenance" in status.render()
    assert status.exits.permitted_in_principle  # a halt never forbids risk-reducing exits
    db.close()


def test_resume_requires_clean_reconciliation(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    with env.db.transaction() as tx:
        TradingStateRepository().halt(
            tx, HaltReason.RECON_POSITION, detail="drift", actor="reconciler", at=NOW
        )
    env.refresh(NOW + timedelta(seconds=1), recon=ReconciliationResult.MISMATCH)
    assert resume(env.db, NOW) is ResumeOutcome.REFUSED
    found = set(env.gates.run_startup(now=NOW).reason_codes)
    assert {"PERSISTENT_HALT", "RECONCILIATION_REQUIRED"} <= found
    env.refresh(NOW + timedelta(seconds=2))
    assert resume(env.db, NOW) is ResumeOutcome.RESUMED
    assert env.gates.run_startup(now=NOW + timedelta(seconds=2)).new_entries_allowed


# -------------------------------------------------------------- loss limits


def test_daily_lockout_expires_at_next_session_open(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    hold(env.db, env.pf, "AAPL", "20", "100.00", at=NOW)
    with env.db.transaction() as tx:
        env.pf.record_mark(tx, mark("AAPL", "94.00", as_of=NOW, received=NOW))
    env.monitor.evaluate(
        now=NOW, verification=verified(env.db, env.pf, NOW)
    )  # 120 loss on 5000: lockout
    env.gates.run_startup(now=NOW)
    assert "DAILY_LOSS_LOCKOUT" in codes(env, NOW)
    close(env.monitor, "AAPL", MON, "94.00")
    for at, locked in [(et(TUE, 9, 0), True), (et(TUE, 9, 31), False)]:
        env.refresh(at)
        env.monitor.establish_baselines(now=at)
        with env.db.transaction() as tx:
            env.pf.record_mark(
                tx, mark("AAPL", "94.00", as_of=at - timedelta(seconds=30), received=at)
            )
        assert ("DAILY_LOSS_LOCKOUT" in codes(env, at)) is locked, at


def test_weekly_restriction_survives_manual_resume(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    hold(env.db, env.pf, "AAPL", "20", "100.00", at=NOW)
    with env.db.transaction() as tx:
        env.pf.record_mark(tx, mark("AAPL", "87.00", as_of=NOW, received=NOW))
    env.monitor.evaluate(now=NOW, verification=verified(env.db, env.pf, NOW))  # 260 >= 250 weekly
    env.gates.run_startup(now=NOW)
    assert {"WEEKLY_LIMIT_ACTIVE", "PERSISTENT_HALT"} <= codes(env, NOW)
    env.refresh(NOW + timedelta(seconds=5))
    assert resume(env.db, NOW) is ResumeOutcome.RESUMED
    after = codes(env, NOW + timedelta(seconds=5))
    assert "PERSISTENT_HALT" not in after
    assert "WEEKLY_LIMIT_ACTIVE" in after  # resume does not bypass the weekly limit
    status = env.gates.evaluate(now=NOW + timedelta(seconds=5))
    assert status.exits.permitted_in_principle


def test_unmapped_upstream_blocker_fails_closed() -> None:
    (finding,) = TradingGates._upstream(["SOMETHING_NEW"])
    assert finding.reason is R.UNMAPPED_BLOCKER and finding.layer is GateLayer.TRANSIENT


def test_observe_mode_with_read_only_adapter_never_allows_entries(tmp_path: Path) -> None:
    obs = Database.create(tmp_path / "observe.sqlite3", RuntimeMode.OBSERVE)
    transport = FakeTransport()
    transport.always("trading", "/account", js({**ACCOUNT, "id": "acct"}))
    transport.always("trading", "/account/configurations", js(CONFIG))
    transport.always("trading", "/clock", js(CLOCK))
    adapter = AlpacaPaperReadAdapter(transport, clock=lambda: NOW, sleep=lambda _: None)
    gates = TradingGates(
        obs,
        VirtualPortfolio(obs, PF_CONFIG),
        LossLimitMonitor(obs, VirtualPortfolio(obs, PF_CONFIG), LOSS_CONFIG),
        adapter,
        replace(GATE_CONFIG, mode=RuntimeMode.OBSERVE),
    )
    status = gates.run_startup(now=NOW)
    assert "RUNTIME_MODE_NO_EXECUTION" in status.reason_codes and not status.new_entries_allowed
    assert all(
        path in {"/account", "/account/configurations", "/clock"} for _, path, _ in transport.calls
    )
    obs.close()


def test_gates_have_no_execution_capability() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "risk"
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {
                    "submit_order",
                    "cancel_order",
                    "replace_order",
                    "replace_order_by_id",
                }, path
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("alpaca"), path
                assert node.module != "swing_trading_bot.broker.alpaca_read", path


def test_gate_config_rejects_execution_mode() -> None:
    with pytest.raises(ValueError, match="paper_execute"):
        replace(GATE_CONFIG, mode=RuntimeMode.PAPER_EXECUTE)
