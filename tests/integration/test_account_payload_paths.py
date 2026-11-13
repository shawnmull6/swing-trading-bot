"""Step 15B.2: unknown ``pattern_day_trader`` through every path, and the dedicated
broker-payload classification (exit 10), offline.

* CLI paths (real ``AlpacaPaperReadAdapter`` over the fake account, whose default
  ``/v2/account`` payload now matches the observed real schema — no PDT field):
  ``broker-check``, ``init-ledger``, reconciliation, runtime startup/sync.
* Simulate-mode paths (``SimConfig.pattern_day_trader``): startup gates, pre-trade and the
  order manager. An unknown value must give exactly the same result as a known one —
  it may neither grant a permission nor bypass a gate.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.broker.sim import SimConfig
from swing_trading_bot.ledger.broker_outage import load_streak
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.oms.order_manager import PlacementOutcome
from tests.support.cli_env import CliEnv, cli_env
from tests.support.gates import healthy_env
from tests.support.oms import oms_env
from tests.support.portfolio import NOW
from tests.support.pretrade import codes, proposal, risk_env

MISSING = object()
PDT_VALUES: list[Any] = [MISSING, None, True, False]
MALFORMED: list[dict[str, Any]] = [
    {"pattern_day_trader": "false"},
    {"pattern_day_trader": 0},
    {"cash": None},  # a safety-critical field: null is treated as missing
    {"trading_blocked": "false"},
    {"cash": 100000.5},  # a JSON float, not a decimal string
]


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def set_pdt(e: CliEnv, value: Any) -> None:
    if value is not MISSING:
        e.account.account_overrides["pattern_day_trader"] = value


def expected(value: Any) -> bool | None:
    return None if value is MISSING else value


def payload_halts(e: CliEnv) -> int:
    with e.open() as db, db.read() as s:
        return sum(1 for x in TradingStateRepository().events(s)
                   if x.action == "HALT" and x.reason_code == "BROKER_PAYLOAD")  # fmt: skip


# ============================================================ broker-check
@pytest.mark.parametrize("value", PDT_VALUES)
def test_broker_check_reports_pdt_and_nothing_else_changes(e: CliEnv, value: Any) -> None:
    set_pdt(e, value)
    code, env = e.run("broker-check")
    assert code == 0, env
    result = env["result"]
    assert result["pattern_day_trader"] is expected(value)
    assert result["identity_problems"] == [] and result["execution_available"] is False
    assert (result["trading_blocked"], result["account_blocked"]) == (False, False)


def test_broker_check_renders_unknown_pdt_as_json_null(e: CliEnv) -> None:
    e.run("broker-check")
    assert '"pattern_day_trader": null' in e.last_text


@pytest.mark.parametrize("override", MALFORMED)
def test_broker_check_payload_violation_exits_10(e: CliEnv, override: dict[str, Any]) -> None:
    e.account.account_overrides.update(override)
    code, env = e.run("broker-check")
    assert code == 10 and env["exit_meaning"] == "BROKER_PAYLOAD"
    assert env["result"]["error"] == "InvalidBrokerDataError"


# ============================================================ init-ledger
@pytest.mark.parametrize("override", MALFORMED)
def test_init_payload_violation_exits_10_and_creates_nothing(e: CliEnv, override: dict[str, Any]
                                                             ) -> None:  # fmt: skip
    e.account.account_overrides.update(override)
    code, env = e.run("init-ledger", "--confirm-path", str(e.ledger), "--confirm-account",
                      e.account.account_id, "--confirm-environment", "paper")  # fmt: skip
    assert code == 10 and env["result"]["status"] == "BROKER_PAYLOAD"
    assert env["result"]["problems"][0].startswith("InvalidBrokerDataError:")
    assert not e.ledger.exists()
    assert sorted(p.name for p in e.data.iterdir()) == ["ledger.sqlite3.lock"]


def test_init_unavailable_broker_still_exits_8(e: CliEnv) -> None:
    e.account.fail("/account", *[ServiceUnavailableError("503")] * 3)
    code, env = e.run("init-ledger")
    assert code == 8 and env["result"]["status"] == "BROKER_UNAVAILABLE"
    assert not e.ledger.exists()


# ============================================================ init, reconcile, startup, sync
@pytest.mark.parametrize("value", PDT_VALUES)
def test_unknown_pdt_through_init_reconcile_resume_and_sync(e: CliEnv, value: Any) -> None:
    set_pdt(e, value)
    e.ready()  # init (exit 0) -> CLEAN reconcile (exit 0) -> resume (exit 0)
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN"
    code, env = e.run("sync")
    assert code == 0, env
    preflight = env["result"]["preflight"]
    assert preflight["ok"] is True and preflight["halted"] is None
    assert env["result"]["trading_state"]["halted"] is False
    assert payload_halts(e) == 0


# ======================================================== persistent BROKER_PAYLOAD halt (15B.3)
def streak_now(e: CliEnv) -> Any:
    with e.open() as db, db.read() as s:
        return load_streak(s)


def trading_state(e: CliEnv) -> tuple[bool, str | None]:
    with e.open() as db, db.read() as s:
        st = TradingStateRepository().get(s)
    return st.halted, st.reason_code


def test_initial_malformed_account_is_exit_10_with_a_persistent_halt(e: CliEnv) -> None:
    e.ready()
    before = streak_now(e)
    e.account.account_overrides["pattern_day_trader"] = "false"
    code, env = e.run("sync")
    assert code == 10 and env["exit_meaning"] == "BROKER_PAYLOAD"
    result = env["result"]
    assert result["preflight"]["halted"] == "BROKER_PAYLOAD"
    assert result["preflight"]["broker_payload_error"] is True
    assert result["halts_raised"] == ["BROKER_PAYLOAD"]
    assert result["sync"] is None  # nothing ingested after a malformed account read
    assert result["broker_outage"]["event"] == "UNCHANGED"
    assert result["broker_outage"]["cycle_failures"] == []  # never an outage category
    assert streak_now(e) == before and trading_state(e) == (True, "BROKER_PAYLOAD")


@pytest.mark.parametrize(
    "argv",
    [("sync",), ("reconcile",), ("resume", "--reason", "try"),
     ("rebaseline", "propose", "--reason", "try")],
)  # fmt: skip
def test_operation_blocked_by_recorded_payload_halt_is_exit_4(e: CliEnv, argv: tuple[str, ...]
                                                              ) -> None:  # fmt: skip
    e.ready()
    e.account.account_overrides["pattern_day_trader"] = "false"
    assert e.run("sync")[0] == 10
    before = streak_now(e)
    code, env = e.run(*argv)
    assert code == 4, env  # was 8 (BROKER_UNAVAILABLE) before Step 15B.3
    assert payload_halts(e) == 1  # not re-recorded
    assert trading_state(e) == (True, "BROKER_PAYLOAD")  # never cleared automatically
    assert streak_now(e) == before  # not counted toward an outage


def test_payload_halt_persists_across_restarts_until_operator_resume(e: CliEnv) -> None:
    e.ready()
    e.account.account_overrides["pattern_day_trader"] = "false"
    assert e.run("sync")[0] == 10
    for _ in range(2):  # every CLI invocation is a fresh process/runtime
        assert e.run("sync")[0] == 4
        assert e.run("halts")[1]["result"]["trading_state"]["reason_code"] == "BROKER_PAYLOAD"
    del e.account.account_overrides["pattern_day_trader"]  # broker payload fixed
    e.account.clock.advance(minutes=1)
    code, env = e.run("sync")
    assert code == 0  # a completed sync does not clear (or fail on) an existing halt
    assert trading_state(e) == (True, "BROKER_PAYLOAD")
    code, env = e.run("resume", "--reason", "payload fixed upstream")
    assert code == 0 and env["result"]["resumed"] is True
    assert trading_state(e)[0] is False


def test_existing_higher_priority_halt_governs_a_payload_error(e: CliEnv) -> None:
    e.ready()
    assert e.run("halt", "--reason", "maintenance")[0] == 0
    e.account.account_overrides["pattern_day_trader"] = "false"
    code, env = e.run("sync")
    assert code == 4  # the ledger was already halted: HALT_OR_MISMATCH, not 10 or 8
    assert env["result"]["preflight"]["broker_payload_error"] is True


def test_outage_breaker_unchanged_after_payload_halt(e: CliEnv) -> None:
    e.ready()
    e.account.account_overrides["pattern_day_trader"] = "false"
    e.run("sync")
    e.run("sync")
    assert streak_now(e) is None  # no outage streak was ever started
    del e.account.account_overrides["pattern_day_trader"]
    e.account.down["*"] = lambda: ServiceUnavailableError("HTTP 503", status_code=503)
    e.account.clock.advance(minutes=1)
    code, env = e.run("sync")
    assert code == 8 and env["result"]["broker_outage"]["event"] == "STARTED"


# ============================================================ gates, pre-trade, order manager
@pytest.mark.parametrize("value", [None, True, False])
def test_startup_gates_identical_for_any_pdt(tmp_path: Path, value: bool | None) -> None:
    env = healthy_env(tmp_path, sim_config=SimConfig(pattern_day_trader=value))
    status = env.gates.run_startup(now=NOW)
    assert status.new_entries_allowed and status.findings == (), status.render()


@pytest.mark.parametrize("value", [None, True, False])
def test_unknown_pdt_does_not_bypass_a_restricted_gate(tmp_path: Path, value: bool | None) -> None:
    config = SimConfig(pattern_day_trader=value, trading_blocked=True)
    env = healthy_env(tmp_path, sim_config=config)
    status = env.gates.run_startup(now=NOW)
    assert not status.new_entries_allowed
    assert "BROKER_ACCOUNT_RESTRICTED" in status.reason_codes


def test_pretrade_decision_is_identical_for_any_pdt(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    base = r.account()
    results = set()
    for value in (None, True, False):
        decision = r.evaluate(proposal(), account=replace(base, pattern_day_trader=value))
        assert decision.approved, decision.rejection_codes
        results.add((decision.approved_qty, decision.reservation, decision.planned_risk))
    assert len(results) == 1


@pytest.mark.parametrize("value", [None, True, False])
def test_unknown_pdt_does_not_bypass_pretrade_restriction(tmp_path: Path,
                                                          value: bool | None) -> None:  # fmt: skip
    r = risk_env(tmp_path)
    blocked = replace(r.account(), trading_blocked=True, pattern_day_trader=value)
    decision = r.evaluate(proposal(), account=blocked)
    assert not decision.approved and "ACCOUNT_RESTRICTED" in codes(decision)


@pytest.mark.parametrize("value", [None, True, False])
def test_order_manager_places_entry_with_any_pdt(tmp_path: Path, value: bool | None) -> None:
    om = oms_env(tmp_path, sim_config=SimConfig(pattern_day_trader=value))
    result = om.place()
    assert result.outcome is PlacementOutcome.ACCEPTED, result
