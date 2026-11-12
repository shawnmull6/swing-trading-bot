"""Step 14: per-ledger OS locks (flock) — exclusivity, crash release, CLI refusal."""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from swing_trading_bot.app.lock import LedgerLock, LockHeldError, LockMode, lock_path_for
from tests.support.cli_env import CliEnv, cli_env
from tests.support.lock_holder import die_holding, hold

SPAWN = mp.get_context("spawn")


def test_exclusive_and_shared_semantics(tmp_path: Path) -> None:
    ledger = tmp_path / "x.sqlite3"
    with (
        LedgerLock(ledger, LockMode.SHARED, command="a"),
        LedgerLock(ledger, LockMode.SHARED, command="b"),
        pytest.raises(LockHeldError),
    ):
        LedgerLock(ledger, LockMode.EXCLUSIVE, command="writer").acquire()
    with LedgerLock(ledger, LockMode.EXCLUSIVE, command="writer") as held:
        assert held.held
        note = json.loads(lock_path_for(ledger).read_text())
        assert note["command"] == "writer" and "diagnostic only" in note["note"]
        for mode in LockMode:
            with pytest.raises(LockHeldError):
                LedgerLock(ledger, mode, command="other").acquire()
    LedgerLock(ledger, LockMode.EXCLUSIVE, command="after").acquire().release()


def test_ownership_is_never_read_from_the_note(tmp_path: Path) -> None:
    ledger = tmp_path / "x.sqlite3"
    lock_path_for(ledger).write_text(json.dumps({"pid": 1, "command": "ghost"}))
    with LedgerLock(ledger, LockMode.EXCLUSIVE, command="real"):  # stale text ignored
        pass


def test_lock_held_by_another_process_refuses_cli(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.init()
    ready_r, ready_w = SPAWN.Pipe(duplex=False)
    release = SPAWN.Event()
    proc = SPAWN.Process(target=hold, args=(str(e.ledger), "exclusive", ready_w, release))
    proc.start()
    try:
        assert ready_r.recv() == "held"
        for argv in (("status",), ("sync",), ("halt", "--reason", "x"), ("reconcile",)):
            code, env = e.run(*argv)
            assert code == 6 and env["exit_meaning"] == "LOCK_HELD", argv
    finally:
        release.set()
        proc.join(30)
    assert e.run("status")[0] == 0


def test_shared_holder_blocks_operations_but_not_inspection(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.init()
    ready_r, ready_w = SPAWN.Pipe(duplex=False)
    release = SPAWN.Event()
    proc = SPAWN.Process(target=hold, args=(str(e.ledger), "shared", ready_w, release))
    proc.start()
    try:
        assert ready_r.recv() == "held"
        assert e.run("status")[0] == 0
        assert e.run("sync")[0] == 6
    finally:
        release.set()
        proc.join(30)


def test_crashed_holder_releases_the_lock(tmp_path: Path) -> None:
    e: CliEnv = cli_env(tmp_path)
    e.init()
    ready_r, ready_w = SPAWN.Pipe(duplex=False)
    proc = SPAWN.Process(target=die_holding, args=(str(e.ledger), ready_w))
    proc.start()
    assert ready_r.recv() == "held"
    proc.join(30)
    assert proc.exitcode == 9
    assert "doomed" in lock_path_for(e.ledger).read_text()  # the note survives ...
    code, _ = e.run("halt", "--reason", "after crash")  # ... but the OS freed the lock
    assert code == 0
