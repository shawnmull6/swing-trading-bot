"""Step 14: crash recovery, restart and idempotency of the runtime sequences.

Idempotency is judged on economic effects and audit history, not on a whole-ledger
fingerprint: repeated runs legitimately append reconciliation runs and refresh sync
markers, but must never duplicate fills, fee bookings, cash entries or activities, nor
change reservations, positions or order state when the broker evidence is unchanged."""

from __future__ import annotations

import multiprocessing as mp
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.app.lock import LedgerLock, LockMode
from swing_trading_bot.app.paths import DataPaths
from swing_trading_bot.app.runtime import reconcile, sync_cycle
from swing_trading_bot.app.simulation import Harness, create_harness, scenario_happy_path
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.oms.reconciler import Outcome
from tests.support.cli_crash import CRASH_EXIT, run_and_die
from tests.support.cli_env import CONFIG_DIR, ECONOMIC, CliEnv, cli_env

D = Decimal
BOOKS = ("fills", "cash_entries", "broker_activities", "positions", "cash_reservations")


class Crash(BaseException):
    pass


def crash_at(point: str):  # type: ignore[no-untyped-def]
    def hook(reached: str) -> None:
        if reached == point:
            raise Crash(point)

    return hook


# ============================================================ crash recovery (CLI)
@pytest.mark.parametrize("point", ["recon:after_observation", "recon:before_persist"])
def test_in_process_crash_during_reconcile_converges(tmp_path: Path, point: str) -> None:
    e = cli_env(tmp_path)
    e.ready()
    runs = e.scalar("SELECT count(*) FROM reconciliation_runs")
    e.crash = crash_at(point)
    e.account.clock.advance(minutes=1)
    with pytest.raises(Crash):
        e.run("reconcile")
    assert e.scalar("SELECT count(*) FROM reconciliation_runs") == runs  # nothing persisted
    e.crash = None
    code, env = e.run("reconcile")  # the lock was released by the unwinding command
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN"
    assert e.scalar("SELECT count(*) FROM reconciliation_runs") == runs + 1


def test_process_killed_during_reconcile_releases_lock_and_recovers(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.ready()
    before = e.fingerprint(ECONOMIC)
    e.account.clock.advance(minutes=1)
    proc = mp.get_context("spawn").Process(
        target=run_and_die,
        args=(str(tmp_path), e.account.clock.now.isoformat(), "reconcile", "recon:before_persist"),
    )
    proc.start()
    proc.join(60)
    assert proc.exitcode == CRASH_EXIT
    with LedgerLock(e.ledger, LockMode.EXCLUSIVE, command="probe"):
        pass  # the killed process's flock is gone
    with e.open() as db, db.read() as s:
        assert integrity_report(s).ok
    assert e.fingerprint(ECONOMIC) == before
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN"


def test_halt_survives_restart_and_startup(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.ready()
    assert e.run("halt", "--reason", "hold")[0] == 0
    for argv in (("sync",), ("reconcile",), ("status",)):
        e.account.clock.advance(minutes=1)
        _code, env = e.run(*argv)
        state = env["result"].get("trading_state") or env["result"]["cycle"]["trading_state"]
        assert state["halted"] is True and state["reason_code"] == "OPERATOR", argv


# ============================================================ idempotency
def test_repeated_observe_runs_append_audit_but_never_book_twice(tmp_path: Path) -> None:
    e: CliEnv = cli_env(tmp_path)
    e.ready()
    e.account.deposit("40.00")
    e.account.clock.advance(minutes=20)
    outcomes, codes = [], []
    halts = None
    for _ in range(3):
        _code, env = e.run("reconcile")
        recon = env["result"]["reconciliation"]
        outcomes.append(recon["outcome"])
        codes.append(sorted(f["code"] for f in recon["findings"]))
        if halts is None:
            halts = e.scalar("SELECT count(*) FROM halt_events")
            books = e.fingerprint(BOOKS)
        e.account.clock.advance(minutes=1)
    assert outcomes == ["MISMATCH"] * 3 and codes[0] == codes[1] == codes[2]
    assert e.fingerprint(BOOKS) == books  # the deposit is preserved once, never booked
    assert e.scalar("SELECT count(*) FROM broker_activities") == 1
    assert e.scalar("SELECT count(*) FROM cash_entries") == 1  # only the virtual deposit
    # Existing step-13 behaviour: every MISMATCH run re-records the halt, referencing its
    # own run (append-only history); the state itself stays RECON_CASH.
    assert e.scalar("SELECT count(*) FROM halt_events") == (halts or 0) + 2
    assert (
        e.scalar(
            "SELECT count(*) FROM halt_events WHERE reason_code = 'RECON_CASH' "
            "AND reconciliation_run_id IS NOT NULL"
        )
        == 3
    )
    assert e.run("halts")[1]["result"]["trading_state"]["reason_code"] == "RECON_CASH"


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    (tmp_path / "data").mkdir()
    paths = DataPaths.at(tmp_path / "data")
    paths.sim_dir.mkdir()
    harness = create_harness(paths, "idem", AppConfig.load(CONFIG_DIR))
    yield harness
    harness.db.close()


def _books(h: Harness) -> tuple[str, list[tuple[str, str, str]], int, int]:
    with h.db.read() as s:
        books = ledger_fingerprint(s, BOOKS)
        rows = s.fetch_all("SELECT client_order_id, status, filled_qty FROM orders")
        orders = [(str(r[0]), str(r[1]), str(r[2])) for r in rows]
        events = int(str(s.scalar("SELECT count(*) FROM order_events")))
        fees = int(str(s.scalar("SELECT count(*) FROM cash_entries WHERE kind = 'FEE'")))
    return books, orders, events, fees


def test_repeated_sim_cycles_never_duplicate_fills_or_fees(h: Harness) -> None:
    scenario_happy_path(h)
    assert all(h.checks.values()), h.checks
    coid = h.summary()["orders"][0]["client_order_id"]
    h.sim.schedule_cash_activity("FEE", D("-0.02"), order_client_id=coid)
    h.advance(timedelta(minutes=1))
    h.cycle("fee posted")
    h.advance(timedelta(minutes=20))
    assert reconcile(h.rt).recon.outcome is Outcome.CLEAN  # type: ignore[union-attr]
    first = _books(h)
    assert first[3] == 1  # exactly one fee booking
    runs = h.rt.db
    outcomes = []
    with runs.read() as s:
        before_runs = int(str(s.scalar("SELECT count(*) FROM reconciliation_runs")))
        before_halts = int(str(s.scalar("SELECT count(*) FROM halt_events")))
    for _ in range(3):
        h.advance(timedelta(minutes=1))
        assert sync_cycle(h.rt).sync is not None
        outcomes.append(reconcile(h.rt).recon.outcome)  # type: ignore[union-attr]
    assert outcomes == [Outcome.CLEAN] * 3
    assert _books(h) == first  # fills, fees, cash, activities, positions, reservations,
    #                            order state and order events all unchanged
    with runs.read() as s:
        assert int(str(s.scalar("SELECT count(*) FROM reconciliation_runs"))) == before_runs + 3
        assert int(str(s.scalar("SELECT count(*) FROM halt_events"))) == before_halts
    assert h.rt.pf.check_invariants(now=h.tick()).ok
