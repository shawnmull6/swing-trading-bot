"""M2-1: account restrictions through gates, pre-trade, the order manager, exit readiness,
reconciliation and the observe CLI (offline: SimBroker and the fake HTTP account).

* A known restriction, or an unknown entry-critical field, blocks NEW entries.
* Exits are never suppressed because entries are prohibited; only restrictions documented
  to prevent every order write make an exit unattemptable, and unknown state is reported.
* M1 OBSERVE/SIMULATE stay report-only for U6; ``require_m2_account_protections`` makes
  protections and verified account state mandatory.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.broker.sim import SimConfig
from swing_trading_bot.domain.enums import HaltReason
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.oms.reconciler import Outcome, Severity
from swing_trading_bot.risk.exits import ExitFailure, ExitReadiness
from swing_trading_bot.risk.gates import GateLayer, GateReason, TradingGates
from swing_trading_bot.risk.pretrade import RejectionCode
from tests.integration.test_exit_readiness import assessor, held_on_both
from tests.support.cli_env import CliEnv, cli_env
from tests.support.gates import GATE_CONFIG, healthy_env
from tests.support.oms import OmsEnv, oms_env
from tests.support.portfolio import NOW
from tests.support.pretrade import codes, proposal, risk_env
from tests.support.recon import ReconEnv, recon_env

D = Decimal
R = GateReason
QUIET = timedelta(minutes=20)
M2 = replace(GATE_CONFIG, require_m2_account_protections=True)

KNOWN_RESTRICTIONS: list[dict[str, Any]] = [
    {"trading_blocked": True},
    {"account_blocked": True},
    {"trade_suspended_by_user": True},
    {"transfers_blocked": True},
    {"suspend_trade": True},
    {"closing_transactions_only": True},
    {"account_status": "INACTIVE"},
]
ENTRY_CRITICAL_UNKNOWN: list[dict[str, Any]] = [
    {"trade_suspended_by_user": None},
    {"closing_transactions_only": None},
]


def ids(params: list[dict[str, Any]]) -> list[str]:
    return [",".join(f"{k}={v}" for k, v in p.items()) for p in params]


# ============================================================ gates (M1 modes)
@pytest.mark.parametrize("changes", KNOWN_RESTRICTIONS, ids=ids(KNOWN_RESTRICTIONS))
def test_known_restriction_blocks_new_entries(tmp_path: Path, changes: dict[str, Any]) -> None:
    status = healthy_env(tmp_path, sim_config=SimConfig(**changes)).gates.run_startup(now=NOW)
    assert not status.new_entries_allowed
    assert "BROKER_ACCOUNT_RESTRICTED" in status.reason_codes
    # Halting new entries is not a reason to suppress risk-reducing exits.
    assert status.exits.permitted_in_principle, status.render()


@pytest.mark.parametrize("changes", ENTRY_CRITICAL_UNKNOWN, ids=ids(ENTRY_CRITICAL_UNKNOWN))
def test_unknown_entry_critical_field_blocks_new_entries(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    status = healthy_env(tmp_path, sim_config=SimConfig(**changes)).gates.run_startup(now=NOW)
    assert status.startup_complete  # a transient condition, not a startup prerequisite
    assert status.reason_codes == ("BROKER_RESTRICTION_UNKNOWN",), status.render()
    assert status.exits.permitted_in_principle


def test_unknown_transfers_blocked_is_advisory_in_m1(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(transfers_blocked=None))
    status = env.gates.run_startup(now=NOW)
    assert status.new_entries_allowed, status.render()
    assert [f.reason for f in status.advisories] == [R.BROKER_RESTRICTION_UNREPORTED]


def test_clear_restrictions_leave_m1_gates_unchanged(tmp_path: Path) -> None:
    status = healthy_env(tmp_path).gates.run_startup(now=NOW)
    assert status.new_entries_allowed and status.findings == ()


def test_restriction_set_mid_session_blocks_the_next_evaluation(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    assert env.gates.run_startup(now=NOW).new_entries_allowed
    env.sim.corrupt_next("get_account_config",
                         lambda raw: {**raw, "closing_transactions_only": True})  # fmt: skip
    assert "BROKER_ACCOUNT_RESTRICTED" in env.gates.evaluate(now=NOW).reason_codes
    assert env.gates.evaluate(now=NOW).new_entries_allowed  # re-derived, not latched


def test_malformed_restriction_is_broker_data_invalid(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env.sim.corrupt_next("get_account", lambda raw: {**raw, "trade_suspended_by_user": "no"})
    status = env.gates.evaluate(now=NOW)
    assert "BROKER_DATA_INVALID" in status.reason_codes and not status.new_entries_allowed
    assert not status.exits.permitted_in_principle  # unreadable broker data: unsafe


# ============================================================ gates (M2 requirement)
def strict(tmp_path: Path, **sim: Any) -> TradingGates:
    env = healthy_env(tmp_path, sim_config=SimConfig(**sim))
    return TradingGates(env.db, env.pf, env.monitor, env.sim, M2)


def test_m2_requirement_passes_with_protections_and_verified_state(tmp_path: Path) -> None:
    status = strict(tmp_path).run_startup(now=NOW)
    assert status.new_entries_allowed and status.findings == (), status.render()


@pytest.mark.parametrize(
    ("sim", "reason"),
    [
        ({"no_shorting": False}, "ACCOUNT_PROTECTIONS_NOT_SET"),
        ({"max_margin_multiplier": D(4)}, "ACCOUNT_PROTECTIONS_NOT_SET"),
        ({"transfers_blocked": None}, "M2_ACCOUNT_STATE_UNVERIFIED"),
        ({"trade_suspended_by_user": None}, "M2_ACCOUNT_STATE_UNVERIFIED"),
        ({"closing_transactions_only": None}, "M2_ACCOUNT_STATE_UNVERIFIED"),
        ({"trade_suspended_by_user": True}, "M2_ACCOUNT_STATE_UNVERIFIED"),
        ({"closing_transactions_only": True}, "M2_ACCOUNT_STATE_UNVERIFIED"),
        ({"transfers_blocked": True}, "M2_ACCOUNT_STATE_UNVERIFIED"),
    ],
)
def test_m2_requirement_blocks_startup(tmp_path: Path, sim: dict[str, Any], reason: str) -> None:
    status = strict(tmp_path, **sim).run_startup(now=NOW)
    assert not status.startup_complete and not status.new_entries_allowed
    assert any(f.reason.value == reason and f.layer is GateLayer.STARTUP
               for f in status.findings), status.render()  # fmt: skip


def test_m2_requirement_fails_closed_when_configuration_is_unreadable(tmp_path: Path) -> None:
    gates = strict(tmp_path)
    for _ in range(4):  # beyond any bounded read retry
        gates._broker.fail_next("get_account_config", ServiceUnavailableError("503"))  # type: ignore[attr-defined]
    status = gates.run_startup(now=NOW)
    startup = {f.reason for f in status.findings if f.layer is GateLayer.STARTUP}
    assert {R.ACCOUNT_PROTECTIONS_NOT_SET, R.M2_ACCOUNT_STATE_UNVERIFIED} <= startup
    assert not status.startup_complete


def test_m1_protections_stay_report_only(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(no_shorting=False, transfers_blocked=None))
    status = env.gates.run_startup(now=NOW)
    assert status.new_entries_allowed
    assert {f.reason for f in status.advisories} == {
        R.ACCOUNT_PROTECTIONS_NOT_SET, R.BROKER_RESTRICTION_UNREPORTED}  # fmt: skip


# ============================================================ pre-trade
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"trade_suspended_by_user": True}, RejectionCode.ACCOUNT_RESTRICTED),
        ({"transfers_blocked": True}, RejectionCode.ACCOUNT_RESTRICTED),
        ({"trading_blocked": True}, RejectionCode.ACCOUNT_RESTRICTED),
        ({"status": "INACTIVE"}, RejectionCode.ACCOUNT_RESTRICTED),
        ({"trade_suspended_by_user": None}, RejectionCode.ACCOUNT_RESTRICTION_UNKNOWN),
    ],
)
def test_pretrade_rejects_restricted_or_unknown_account(
    tmp_path: Path, changes: dict[str, Any], code: RejectionCode
) -> None:
    r = risk_env(tmp_path)
    decision = r.evaluate(proposal(), account=replace(r.account(), **changes))
    assert not decision.approved and code in codes(decision)


def test_pretrade_unknown_transfers_blocked_is_not_an_account_rejection(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    decision = r.evaluate(proposal(), account=replace(r.account(), transfers_blocked=None))
    assert decision.approved, decision.rejection_codes


@pytest.mark.parametrize("value", [True, None])
def test_pretrade_sees_config_restrictions_via_gate(tmp_path: Path, value: bool | None) -> None:
    r = risk_env(tmp_path)
    r.env.sim.config = replace(r.env.sim.config, closing_transactions_only=value)
    decision = r.evaluate(proposal(), gate=r.gate(NOW))
    assert not decision.approved and RejectionCode.GATE_BLOCKED in codes(decision)


# ============================================================ order manager
@pytest.fixture
def o(tmp_path: Path) -> OmsEnv:
    return oms_env(tmp_path)


@pytest.mark.parametrize(
    ("method", "mutation"),
    [
        ("get_account", {"trade_suspended_by_user": True}),
        ("get_account", {"trade_suspended_by_user": None}),
        ("get_account_config", {"closing_transactions_only": True}),
        ("get_account_config", {"closing_transactions_only": None}),
    ],
)
def test_order_manager_writes_nothing_under_a_restriction(
    o: OmsEnv, method: str, mutation: dict[str, Any]
) -> None:
    d = o.decide()
    assert d.approved
    for _ in range(2):  # both the placement account read and the fresh gate evaluation
        o.sim.corrupt_next(method, lambda raw, m=mutation: {**raw, **m})  # type: ignore[misc]
    r = o.om.place_entry(d, now=o.now())
    assert r.refusal is not None, r
    # account fields: pre-trade codes; configuration fields: the fresh gate blocks
    assert any(c in r.detail for c in ("ACCOUNT_RESTRICT", "GATE_BLOCKED")), r
    assert o.submits() == []


# ============================================================ exit readiness
def exit_env(tmp_path: Path, **sim: Any) -> ExitReadiness:
    env = healthy_env(tmp_path, sim_config=SimConfig(**sim))
    held_on_both(env)
    return assessor(env).assess("AAPL", now=NOW)


@pytest.mark.parametrize("changes", [{"trade_suspended_by_user": True}, {"suspend_trade": True}])
def test_exit_reports_broker_order_suspension(tmp_path: Path, changes: dict[str, Any]) -> None:
    r = exit_env(tmp_path, **changes)
    assert {f.code for f in r.failures} == {ExitFailure.BROKER_ORDERS_SUSPENDED}
    assert not r.safe_to_attempt and r.confirmed_qty is None


@pytest.mark.parametrize(
    ("changes", "entry_only"),
    [
        ({"closing_transactions_only": True}, "BROKER_CLOSING_TRANSACTIONS_ONLY"),
        ({"transfers_blocked": True}, "BROKER_TRANSFERS_BLOCKED"),
    ],
)
def test_entry_only_restrictions_never_block_exits(
    tmp_path: Path, changes: dict[str, Any], entry_only: str
) -> None:
    r = exit_env(tmp_path, **changes)
    assert r.safe_to_attempt, r.failures
    assert entry_only in r.entry_only_restrictions
    assert r.confirmed_qty == D(10)


def test_closing_only_semantics_are_reported_as_unverified(tmp_path: Path) -> None:
    r = exit_env(tmp_path, closing_transactions_only=True)
    assert "closing_transactions_only=true: semantics not confirmed" in (
        r.broker_eligibility_unverified
    )


@pytest.mark.parametrize("key", ["trade_suspended_by_user", "transfers_blocked",
                                 "closing_transactions_only"])  # fmt: skip
def test_unknown_restriction_is_reported_not_a_failure(tmp_path: Path, key: str) -> None:
    r = exit_env(tmp_path, **{key: None})
    assert r.safe_to_attempt, r.failures
    assert f"{key} not reported" in r.broker_eligibility_unverified


def test_unreadable_configuration_is_reported_not_a_failure(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    held_on_both(env)
    for _ in range(4):
        env.sim.fail_next("get_account_config", ServiceUnavailableError("503"))
    r = assessor(env).assess("AAPL", now=NOW)
    assert r.safe_to_attempt, r.failures
    assert any("configuration unavailable" in n for n in r.broker_eligibility_unverified)


def test_healthy_exit_has_no_unverified_eligibility(tmp_path: Path) -> None:
    r = exit_env(tmp_path)
    assert r.safe_to_attempt and r.broker_eligibility_unverified == ()
    assert r.broker_eligibility_confirmed


@pytest.mark.parametrize("changes", [{"trade_suspended_by_user": None},
                                     {"closing_transactions_only": None},
                                     {"closing_transactions_only": True}])  # fmt: skip
def test_unknown_eligibility_is_never_confirmed_permission(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    r = exit_env(tmp_path, **changes)
    assert r.safe_to_attempt  # not suppressed ...
    assert not r.broker_eligibility_confirmed  # ... but never confirmed broker permission
    assert r.broker_eligibility_unverified


def test_write_preventing_restriction_is_not_entry_only(tmp_path: Path) -> None:
    r = exit_env(tmp_path, trade_suspended_by_user=True)
    assert "BROKER_ORDERS_SUSPENDED" not in r.entry_only_restrictions
    assert not r.broker_eligibility_confirmed


def test_halt_plus_entry_restriction_still_allows_exit(tmp_path: Path) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(closing_transactions_only=True))
    held_on_both(env)
    with env.db.transaction() as tx:
        TradingStateRepository().halt(tx, HaltReason.OPERATOR, detail="t", actor="t", at=NOW)
    r = assessor(env).assess("AAPL", now=NOW)
    assert r.safe_to_attempt, r.failures
    assert {"PERSISTENT_HALT:OPERATOR", "BROKER_CLOSING_TRANSACTIONS_ONLY"} <= set(
        r.entry_only_restrictions
    )


# ============================================================ reconciliation
def restricted_recon(tmp_path: Path, **sim: Any) -> ReconEnv:
    """The fixtures require a healthy account to set up; restrict the broker afterwards."""
    r = recon_env(tmp_path)
    r.o.sim.config = replace(r.o.sim.config, **sim)
    return r


@pytest.mark.parametrize(
    "sim",
    [{"trade_suspended_by_user": True}, {"transfers_blocked": True}, {"suspend_trade": True},
     {"closing_transactions_only": True}],
)  # fmt: skip
def test_reconciliation_records_order_restrictions_as_advisory(
    tmp_path: Path, sim: dict[str, Any]
) -> None:
    rep = restricted_recon(tmp_path, **sim).run(QUIET)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert rep.codes(Severity.ADVISORY) == {"ACCOUNT_ORDER_RESTRICTION"}
    assert rep.halted is None


@pytest.mark.parametrize("key", ["trade_suspended_by_user", "transfers_blocked",
                                 "closing_transactions_only"])  # fmt: skip
def test_reconciliation_records_unreported_restrictions_as_advisory(
    tmp_path: Path, key: str
) -> None:
    rep = restricted_recon(tmp_path, **{key: None}).run(QUIET)
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert rep.codes(Severity.ADVISORY) == {"ACCOUNT_RESTRICTION_UNREPORTED"}


@pytest.mark.parametrize(
    ("sim", "pretrade_code"),
    [({"trade_suspended_by_user": True}, RejectionCode.ACCOUNT_RESTRICTED),
     ({"trade_suspended_by_user": None}, RejectionCode.ACCOUNT_RESTRICTION_UNKNOWN),
     ({"closing_transactions_only": True}, RejectionCode.GATE_BLOCKED)],
)  # fmt: skip
def test_clean_reconciliation_never_overrides_a_restriction(
    tmp_path: Path, sim: dict[str, Any], pretrade_code: RejectionCode
) -> None:
    r = restricted_recon(tmp_path, **sim)
    rep = r.run(QUIET)
    assert rep.outcome is Outcome.CLEAN and rep.halted is None
    now = r.o.now()
    gate = r.o.r.gate(now)
    assert not gate.new_entries_allowed
    assert {"BROKER_ACCOUNT_RESTRICTED", "BROKER_RESTRICTION_UNKNOWN"} & set(gate.reason_codes)
    decision = r.o.r.evaluate(proposal(), at=now, gate=gate,
                              account=r.o.sim.get_account())  # fmt: skip
    assert not decision.approved and pretrade_code in codes(decision)
    assert RejectionCode.RECONCILIATION_NOT_CLEAN not in codes(decision)  # recon is clean


def test_reconciliation_keeps_blocked_account_inconclusive(tmp_path: Path) -> None:
    rep = restricted_recon(tmp_path, trading_blocked=True).run(QUIET)
    assert rep.outcome is Outcome.INCONCLUSIVE
    assert "ACCOUNT_RESTRICTED" in rep.codes(Severity.INCONCLUSIVE)


def test_reconciliation_clean_without_restrictions_has_no_new_findings(tmp_path: Path) -> None:
    rep = recon_env(tmp_path).run(QUIET)
    assert rep.outcome is Outcome.CLEAN and rep.findings == ()


# ============================================================ observe CLI (real adapter)
@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def gate_codes(e: CliEnv) -> list[str]:
    code, env = e.run("status", "--broker")
    assert code == 0, env
    return list(env["result"]["gates"]["reason_codes"])


def test_observed_payloads_leave_observe_gates_unchanged(e: CliEnv) -> None:
    e.ready()
    found = gate_codes(e)
    assert "BROKER_ACCOUNT_RESTRICTED" not in found
    assert "BROKER_RESTRICTION_UNKNOWN" not in found
    assert "M2_ACCOUNT_STATE_UNVERIFIED" not in found


@pytest.mark.parametrize(
    ("where", "key"),
    [("account", "trade_suspended_by_user"), ("config", "closing_transactions_only")],
)
def test_observe_reports_an_unreported_field_without_halting(
    e: CliEnv, where: str, key: str
) -> None:
    e.ready()
    overrides = e.account.account_overrides if where == "account" else e.account.config_overrides
    overrides[key] = None
    assert "BROKER_RESTRICTION_UNKNOWN" in gate_codes(e)
    e.account.clock.advance(minutes=1)
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN", env
    with e.open() as db, db.read() as s:
        assert not TradingStateRepository().get(s).halted


@pytest.mark.parametrize(
    ("where", "key"),
    [("account", "trade_suspended_by_user"), ("account", "transfers_blocked"),
     ("config", "closing_transactions_only")],
)  # fmt: skip
def test_malformed_field_fails_closed_with_exit_10(e: CliEnv, where: str, key: str) -> None:
    overrides = e.account.account_overrides if where == "account" else e.account.config_overrides
    overrides[key] = "false"
    code, env = e.run("broker-check")
    assert code == 10 and env["result"]["error"] == "InvalidBrokerDataError"
    code, env = e.run("init-ledger", "--confirm-path", str(e.ledger), "--confirm-account",
                      e.account.account_id, "--confirm-environment", "paper")  # fmt: skip
    assert code == 10 and env["result"]["status"] == "BROKER_PAYLOAD"
    assert not e.ledger.exists()


def test_malformed_field_on_a_running_ledger_halts_broker_payload(e: CliEnv) -> None:
    e.ready()
    e.account.account_overrides["trade_suspended_by_user"] = 0
    code, _env = e.run("reconcile")
    assert code == 10
    with e.open() as db, db.read() as s:
        state = TradingStateRepository().get(s)
    assert state.halted and state.reason_code == "BROKER_PAYLOAD"


def test_init_preview_reports_restriction_fields(e: CliEnv) -> None:
    e.account.config_overrides["closing_transactions_only"] = None
    code, env = e.run("init-ledger")
    assert code == 3
    evidence = env["result"]["evidence"]
    assert evidence["trade_suspended_by_user"] is False
    assert evidence["transfers_blocked"] is False
    assert evidence["suspend_trade"] is False
    assert evidence["closing_transactions_only"] is None  # unknown, never false


def test_broker_check_reports_restrictions_without_changing_its_exit(e: CliEnv) -> None:
    code, env = e.run("broker-check")
    result = env["result"]
    assert code == 0 and result["execution_available"] is False
    assert (result["trade_suspended_by_user"], result["transfers_blocked"],
            result["suspend_trade"], result["closing_transactions_only"]) == (
        False, False, False, False)  # fmt: skip
    assert result["new_entries_restricted"] is False
    assert result["m2_entry_problems"] == []
    e.account.account_overrides["trade_suspended_by_user"] = None
    e.account.config_overrides.update(no_shorting=False, closing_transactions_only=True)
    code, env = e.run("broker-check")
    result = env["result"]
    assert code == 0  # reported only: the exit still reflects identity alone
    assert result["trade_suspended_by_user"] is None
    assert result["new_entries_restricted"] is True
    assert "no_shorting=false (shorting enabled)" in result["m2_entry_problems"]
    assert "restriction active: closing_transactions_only=true" in result["m2_entry_problems"]
    assert "restriction not reported: trade_suspended_by_user" in result["m2_entry_problems"]
