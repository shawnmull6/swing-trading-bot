"""Step 14: the command side-effect matrix and the simulation/observe isolation rules."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from swing_trading_bot.app.safety import COMMAND_SAFETY, INSPECTION_COMMANDS, Broker, safety
from swing_trading_bot.app.sim_calendar import SIM_CALENDAR_SOURCE, synthetic_calendars
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.calendar_store import CalendarStore, FetchStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import ModeMismatchError
from tests.support.alpaca_account import START
from tests.support.cli_env import CliEnv, cli_env

APP = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "app"
EXECUTION_NAMES = {
    "SimulationExecution",
    "OrderManager",
    "SimBroker",
    "register_simulation_double",
    "submit_order",
    "cancel_order",
    "place_entry",
    "request_cancel",
}


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.ready()
    return env


# ============================================================ side-effect matrix
@pytest.mark.parametrize("command", sorted(INSPECTION_COMMANDS))
def test_inspection_commands_have_no_side_effects(e: CliEnv, command: str) -> None:
    row = safety(command)
    assert row.broker is Broker.NONE and row.oms_recovery.value == "no"
    before, calls = e.fingerprint(), len(e.account.transport.calls)
    e.account.clock.advance(hours=1)  # recovery would be due by now; it must not run
    code, _ = e.run(command)
    assert code == 0
    assert e.fingerprint() == before  # every ledger table, byte for byte
    assert len(e.account.transport.calls) == calls  # no broker request at all


def test_inspection_reports_a_halt_successfully(e: CliEnv) -> None:
    assert e.run("halt", "--reason", "x")[0] == 0
    code, env = e.run("status")
    assert code == 0 and env["result"]["trading_state"]["halted"] is True


def test_operational_commands_follow_their_documented_effects(e: CliEnv) -> None:
    halts = e.scalar("SELECT count(*) FROM halt_events")
    runs = e.scalar("SELECT count(*) FROM reconciliation_runs")
    e.account.clock.advance(minutes=1)
    assert e.run("sync")[0] == 0
    assert e.scalar("SELECT count(*) FROM reconciliation_runs") == runs  # sync: no recon
    assert e.run("reconcile")[0] == 0
    assert e.scalar("SELECT count(*) FROM reconciliation_runs") == runs + 1
    assert e.scalar("SELECT count(*) FROM halt_events") == halts  # clean: no halt
    assert e.run("halt", "--reason", "x")[0] == 0
    assert e.scalar("SELECT count(*) FROM halt_events") == halts + 1


def test_safety_matrix_covers_every_command() -> None:
    commands = {c.command for c in COMMAND_SAFETY}
    assert commands >= INSPECTION_COMMANDS
    assert all(c.lock is not None for c in COMMAND_SAFETY)
    for c in COMMAND_SAFETY:
        if c.command in INSPECTION_COMMANDS | {"status --broker", "backup"}:
            assert c.lock is not None and c.lock.value == "shared"
        else:
            assert c.lock is not None and c.lock.value == "exclusive"


# ============================================================ execution isolation
def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(a.name for a in node.names)
    return found


def test_only_the_simulation_module_constructs_execution() -> None:
    for path in APP.glob("*.py"):
        if path.name == "simulation.py":
            assert {"SimulationExecution", "OrderManager", "SimBroker"} <= _names(path)
            continue
        leaked = _names(path) & EXECUTION_NAMES
        assert not leaked, f"{path.name} references {sorted(leaked)}"


def test_simulation_module_is_imported_lazily_only_by_simulate() -> None:
    tree = ast.parse((APP / "commands.py").read_text())
    top = {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module is not None}
    assert "swing_trading_bot.app.simulation" not in top
    for path in APP.glob("*.py"):
        if path.name in {"commands.py", "simulation.py"}:
            continue
        imported = {
            n.module
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.ImportFrom) and n.module is not None
        }
        assert "swing_trading_bot.app.simulation" not in imported, path.name


def test_app_never_loads_credentials_or_env_files() -> None:
    for path in APP.glob("*.py"):
        text = path.read_text()
        for needle in ("load_settings", "dotenv", '".env"', "from_settings", "os.environ"):
            assert needle not in text, f"{path.name} uses {needle}"


# ============================================================ simulation isolation
def test_synthetic_calendar_is_never_observe_evidence(tmp_path: Path) -> None:
    trading, settlement = synthetic_calendars(
        START.date() - timedelta(days=7), START.date() + timedelta(days=60), fetched_at=START
    )
    assert trading.source == SIM_CALENDAR_SOURCE
    observe = Database.create(tmp_path / "o.sqlite3", RuntimeMode.OBSERVE, now=START)
    sim = Database.create(tmp_path / "s.sqlite3", RuntimeMode.SIMULATE, now=START)
    store = CalendarStore()
    with observe.transaction() as tx:
        results = [store.record_fetch(tx, f) for f in (trading, settlement)]
    assert all(r.status is FetchStatus.REJECTED for r in results)
    assert "not authoritative" in results[0].problems[0]
    with observe.read() as s:
        assert s.scalar("SELECT count(*) FROM market_calendar") == 0  # no coverage
    with sim.transaction() as tx:
        assert all(store.record_fetch(tx, f).accepted for f in (trading, settlement))
    observe.close()
    sim.close()


def test_simulation_ledgers_stay_apart_from_the_observe_ledger(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    code, env = e.run("simulate", "happy-path", "--run-id", "r1")
    assert code == 0 and env["hypothetical"] is True
    sim_path = e.data / "sim" / "r1.sqlite3"
    assert sim_path.is_file() and not e.ledger.exists()
    with pytest.raises(ModeMismatchError):
        Database.open(sim_path, RuntimeMode.OBSERVE, auto_migrate=False)
    code, env = e.run("status", "--sim-run", "r1")
    assert code == 0 and env["hypothetical"] is True and env["result"]["mode"] == "simulate"
    code, env = e.run("status")  # the observe ledger was never created by simulate
    assert code == 9
    code, env = e.run("simulate", "happy-path", "--run-id", "r1")
    assert code == 9 and "already exists" in env["result"]["detail"]
    e.init()  # sim/ is an expected entry of the data directory
