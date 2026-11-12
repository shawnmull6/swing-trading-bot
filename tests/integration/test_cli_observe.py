"""Step 14: the observe-mode CLI end to end, offline (real adapter, fake HTTP account).

Covers safe initialization, the startup / sync / reconcile sequences, operator halt,
resume and rebaseline, identity and broker failures, and exit-code semantics."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.errors import AuthenticationError, ServiceUnavailableError
from swing_trading_bot.ledger.repositories import (
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import LAST_SUCCESSFUL_SYNC
from tests.support.alpaca_account import ACCOUNT_ID, READ_PATHS
from tests.support.alpaca_fake import order
from tests.support.cli_env import ECONOMIC, CliEnv, cli_env

D = Decimal
POSITION = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "150.2500", "side": "long",
            "current_price": "151.10", "market_value": "1511.00"}  # fmt: skip


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def confirm(e: CliEnv, account: str = ACCOUNT_ID) -> tuple[str, ...]:
    return ("--confirm-path", str(e.ledger), "--confirm-account", account,
            "--confirm-environment", "paper")  # fmt: skip


def halt_actions(e: CliEnv) -> list[tuple[str, str]]:
    with e.open() as db, db.read() as s:
        return [(x.action, x.reason_code) for x in TradingStateRepository().events(s)]


# ============================================================ initialization
def test_init_requires_explicit_confirmation_and_creates_nothing_without_it(e: CliEnv) -> None:
    code, env = e.run("init-ledger")
    assert code == 3 and env["result"]["status"] == "CONFIRMATION_REQUIRED"
    required = env["result"]["required_confirmation"]
    assert required == {
        "confirm_path": str(e.ledger),
        "confirm_account": ACCOUNT_ID,
        "confirm_environment": "paper",
    }
    assert not e.ledger.exists()
    code, env = e.run("init-ledger", *confirm(e, account="someone-else"))
    assert code == 3 and not e.ledger.exists()
    assert sorted(p.name for p in e.data.iterdir()) == ["ledger.sqlite3.lock"]


def test_init_creates_halted_ledger_with_separate_broker_baseline(e: CliEnv) -> None:
    created = e.init()["result"]["created"]
    assert created["halted"] is True and created["halt_reason"] == "INITIALIZED"
    assert created["virtual_initial_capital"] == "5000"
    with e.open() as db, db.read() as s:
        cash = s.scalar("SELECT sum(CAST(amount AS REAL)) FROM cash_entries")
        baseline = s.fetch_one("SELECT reason, cash FROM broker_baselines")
        identity = SyncStateRepository().get(s, "broker.account_id")
    assert cash == 5000.0  # the virtual deposit
    assert baseline == ("INIT", "100000.00")  # the broker's cash, a separate record
    assert identity == ACCOUNT_ID
    assert e.ledger.stat().st_mode & 0o777 == 0o600
    assert not [p for p in e.data.iterdir() if ".init-" in p.name]  # temp file gone
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 3 and "already exists" in env["result"]["problems"][0]


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (lambda a: a.positions.append(POSITION), "not flat"),
        (lambda a: a.open_orders.append(order("ext-1")), "open order"),
        (lambda a: a.account_overrides.update(pending_reg_taf_fees="0.02"), "fees are pending"),
        (lambda a: a.account_overrides.update(trading_blocked=True), "restricted"),
        (lambda a: a.account_overrides.update(currency="EUR"), "currency"),
    ],
)
def test_init_refuses_unsafe_account_state(e: CliEnv, setup: object, problem: str) -> None:
    setup(e.account)  # type: ignore[operator]
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 3, env
    assert any(problem in p for p in env["result"]["problems"]), env["result"]["problems"]
    assert not e.ledger.exists()


def test_init_refuses_unexpected_data_directory_contents(e: CliEnv) -> None:
    (e.data / "notes.txt").write_text("x")
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 3 and "unexpected entries: notes.txt" in env["result"]["problems"][0]
    (e.data / "notes.txt").unlink()
    (e.data / "ledger.sqlite3-wal").write_text("")
    code, env = e.run("init-ledger")
    assert code == 3 and "already exists" in env["result"]["problems"][0]


def test_init_broker_unavailable_creates_nothing(e: CliEnv) -> None:
    e.account.fail("/positions", *[ServiceUnavailableError("503")] * 4)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 8 and env["result"]["status"] == "BROKER_UNAVAILABLE"
    assert not e.ledger.exists()


@pytest.mark.parametrize(
    "argv",
    [("status",), ("sync",), ("reconcile",), ("halts",), ("resume", "--reason", "x")],
)
def test_no_command_initializes_a_ledger_implicitly(e: CliEnv, argv: tuple[str, ...]) -> None:
    code, env = e.run(*argv)
    assert code == 9 and env["result"]["error"] == "DatabaseNotFoundError"
    assert not e.ledger.exists()


def test_missing_data_directory(tmp_path: Path) -> None:
    e = CliEnv(tmp_path)  # data/ never created
    code, _ = e.run("init-ledger")
    assert code == 3
    assert not e.data.exists()


# ============================================================ operations
def test_full_operational_lifecycle_is_read_only_at_the_broker(e: CliEnv) -> None:
    e.ready()
    code, env = e.run("status")
    assert code == 0 and env["result"]["trading_state"]["halted"] is False
    assert env["result"]["execution_available"] is False
    assert "cash" not in env["result"]["broker"]  # broker balances are never shown
    assert e.account.paths_called() <= READ_PATHS
    with e.open() as db, db.read() as s:
        assert SyncStateRepository().get(s, LAST_SUCCESSFUL_SYNC) is not None
    actions = halt_actions(e)
    assert actions == [("HALT", "INITIALIZED"), ("RESUME", "OPERATOR_RESUME")]


def test_resume_refused_without_clean_reconciliation(e: CliEnv) -> None:
    e.init()
    e.account.clock.advance(minutes=20)
    e.account.account_overrides["pending_reg_taf_fees"] = "0.02"  # evidence incomplete
    code, env = e.run("resume", "--reason", "too early")
    assert code == 5 and env["result"]["resumed"] is False
    assert env["result"]["reconciliation"]["outcome"] == "INCONCLUSIVE"
    assert halt_actions(e)[-1][0] == "RESUME_REFUSED"


def test_external_deposit_mismatch_rebaseline_and_resume(e: CliEnv) -> None:
    e.ready()
    e.account.deposit("250.00")
    e.account.clock.advance(minutes=20)
    code, env = e.run("reconcile")
    assert code == 4
    assert env["result"]["reconciliation"]["outcome"] == "MISMATCH"
    assert env["result"]["reconciliation"]["halted"] == "RECON_CASH"
    code, env = e.run("status")
    assert code == 0 and env["result"]["trading_state"]["reason_code"] == "RECON_CASH"
    code, env = e.run("resume", "--reason", "nope")
    assert code == 4 and env["result"]["resumed"] is False
    code, env = e.run("rebaseline", "propose", "--reason", "owner deposit")
    assert code == 0 and env["result"]["unexplained_amount_to_acknowledge"] == "250.00"
    rid = str(env["result"]["request_id"])
    e.account.clock.advance(minutes=1)
    code, env = e.run("rebaseline", "approve", rid, "--acknowledge-unexplained", "25.00",
                      "--reason", "typo")  # fmt: skip
    assert code == 3 and env["result"]["decision"] == "REFUSED"
    code, env = e.run("rebaseline", "approve", rid, "--acknowledge-unexplained", "250.00",
                      "--reason", "statement checked")  # fmt: skip
    assert code == 0 and env["result"]["decision"] == "APPROVED"
    code, env = e.run("halts")
    assert env["result"]["trading_state"]["reason_code"] == "REBASELINE"  # halt kept
    e.account.clock.advance(minutes=2)
    code, env = e.run("resume", "--reason", "clean against new baseline")
    assert code == 0 and env["result"]["resumed"] is True
    code, env = e.run("portfolio")
    assert env["result"]["cash"] == "5000"  # the deposit never became virtual capital


def test_rebaseline_propose_refused_when_not_halted(e: CliEnv) -> None:
    e.ready()
    code, env = e.run("rebaseline", "propose", "--reason", "why")
    assert code == 3 and env["result"]["decision"] == "REFUSED"


def test_operator_halt_persists_and_needs_clean_resume(e: CliEnv) -> None:
    e.ready()
    code, env = e.run("halt", "--reason", "maintenance", actor="operator:alice")
    assert code == 0 and env["result"]["trading_state"]["reason_code"] == "OPERATOR"
    assert env["result"]["trading_state"]["changed_by"] == "operator:alice"
    code, env = e.run("sync")
    assert code == 0  # an existing halt does not fail a completed sync
    assert env["result"]["trading_state"]["halted"] is True  # and is never cleared by it
    code, env = e.run("resume", "--reason", "done")
    assert code == 0 and env["result"]["resumed"] is True


def test_identity_mismatch_halts_before_anything_is_ingested(e: CliEnv) -> None:
    e.ready()
    e.account.account_id = "another-account"
    e.account.deposit("10.00")
    before = e.fingerprint(ECONOMIC)
    code, env = e.run("sync")
    assert code == 4 and env["result"]["preflight"]["halted"] == "RECON_ACCOUNT"
    assert env["result"]["sync"] is None
    assert e.fingerprint(ECONOMIC) == before  # nothing ingested from the wrong account
    code, _ = e.run("sync")
    assert code == 4
    assert halt_actions(e).count(("HALT", "RECON_ACCOUNT")) == 1  # not re-recorded


def test_auth_failure_halts_and_transient_failure_does_not(e: CliEnv) -> None:
    e.ready()
    e.account.fail("/account", *[ServiceUnavailableError("503")] * 3)
    code, env = e.run("sync")
    assert code == 8 and env["result"]["preflight"]["halted"] is None
    assert e.run("halts")[1]["result"]["trading_state"]["halted"] is False
    e.account.fail("/account", AuthenticationError("401", status_code=401))
    code, env = e.run("sync")
    assert code == 4 and env["result"]["preflight"]["halted"] == "AUTH"


def test_inconclusive_reconciliation_is_never_reported_clean(e: CliEnv) -> None:
    e.ready()
    e.account.account_overrides["pending_reg_taf_fees"] = "0.03"
    code, env = e.run("reconcile")
    assert code == 5
    assert env["result"]["reconciliation"]["outcome"] == "INCONCLUSIVE"
    assert env["exit_meaning"] == "INCONCLUSIVE"


def test_resolve_order_refuses_unknown_order(e: CliEnv) -> None:
    e.ready()
    code, env = e.run("resolve-order", "stb-unknown", "--confirm-absent")
    assert code == 3 and env["result"]["problem"] == "no such order in this ledger"


def test_status_broker_evaluates_gates_without_writing(e: CliEnv) -> None:
    e.ready()
    before = e.fingerprint()
    calls = len(e.account.transport.calls)
    code, env = e.run("status", "--broker")
    assert code == 0
    assert "RUNTIME_MODE_NO_EXECUTION" in env["result"]["gates"]["reason_codes"]
    assert e.fingerprint() == before
    assert {p for _s, p, _q in e.account.transport.calls[calls:]} <= {
        "/account", "/account/configurations", "/clock"}  # fmt: skip


def test_broker_commands_refuse_without_configured_access(tmp_path: Path) -> None:
    e = cli_env(tmp_path, broker=False)
    for argv in (("init-ledger",), ("sync",), ("reconcile",), ("status", "--broker")):
        code, env = e.run(*argv)
        assert code == 7, argv
        assert env["result"]["error"] == "BrokerNotConfiguredError"
    assert not e.ledger.exists()


def test_sync_records_marks_only_for_held_symbols(e: CliEnv) -> None:
    e.ready()
    e.account.prices["AAPL"] = D("150.00")
    code, env = e.run("sync")
    assert code == 0 and env["result"]["marks"]["requested"] == []  # flat: nothing held
