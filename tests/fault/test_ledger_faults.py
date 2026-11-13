"""Step 15A fault injection: ledger corruption, migration and backup interruption, and the
manual restore procedure (D15-8). Offline; temporary directories only."""

from __future__ import annotations

import multiprocessing as mp
import shutil
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.backup import BackupVerificationError, content_digest, verify_backup
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import MigrationError
from swing_trading_bot.ledger.migration_guard import migrate_ledger
from swing_trading_bot.ledger.migrator import packaged_migrations
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from tests.support.cli_crash import CRASH_EXIT, backup_and_die
from tests.support.cli_env import CliEnv, cli_env

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
ALL = packaged_migrations()


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.init()
    return env


def digest(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return content_digest(conn)
    finally:
        conn.close()


def damage(path: Path, start: int, length: int, byte: int = 0xAA) -> None:
    with path.open("r+b") as f:
        f.seek(start)
        f.write(bytes([byte]) * length)


# ============================================================ corruption
@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: damage(p, 4096, 4096 * 4),  # b-tree pages overwritten
        lambda p: p.write_bytes(p.read_bytes()[: p.stat().st_size // 2]),  # truncated
        lambda p: damage(p, 0, 16, 0x00),  # header destroyed
        lambda p: p.write_bytes(b""),  # empty file
    ],
)
def test_corrupted_ledger_is_refused_everywhere(e: CliEnv, corrupt: object) -> None:
    corrupt(e.ledger)  # type: ignore[operator]
    before = e.ledger.read_bytes()
    for argv in (("status",), ("sync",), ("reconcile",), ("halts",),
                 ("verify-ledger", str(e.ledger))):  # fmt: skip
        code, _ = e.run(*argv)
        assert code == 9, argv
    assert e.ledger.read_bytes() == before  # nothing tried to "repair" it


def test_foreign_sqlite_file_is_refused(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    conn = sqlite3.connect(e.ledger)
    conn.execute("CREATE TABLE notes (x TEXT)")
    conn.commit()
    conn.close()
    for argv in (("status",), ("verify-ledger", str(e.ledger)), ("migrate", "--backup",
                                                                 str(tmp_path / "b"))):  # fmt: skip
        assert e.run(*argv)[0] == 9, argv


# ============================================================ migration interruption
def _old(path: Path, version: int) -> None:
    db = Database.create(path, RuntimeMode.OBSERVE, now=NOW, migrations=ALL[:version])
    with db.transaction() as tx:
        VirtualPortfolio.initialize(tx, name="main", mode=RuntimeMode.OBSERVE,
                                    initial_capital=Decimal(5000),
                                    max_capital_allocation=Decimal(5000),
                                    account_profile="cash_conservative", at=NOW)  # fmt: skip
    db.close()


@pytest.mark.parametrize("version", [1, len(ALL) - 1])
def test_migration_interrupted_at_every_statement_is_all_or_nothing(
    tmp_path: Path, version: int
) -> None:
    points = [(m.version, i) for m in ALL[version:] for i in range(len(m.statements()))]
    assert points
    for n, point in enumerate(points):
        path = tmp_path / f"l{n}.sqlite3"
        _old(path, version)
        before = digest(path)

        def boom(v: int, i: int, at: tuple[int, int] = point) -> None:
            if (v, i) == at:
                raise RuntimeError("injected")

        with pytest.raises(MigrationError):
            migrate_ledger(path, mode=RuntimeMode.OBSERVE, backup_path=tmp_path / f"b{n}",
                           migrations=ALL, now=NOW, before_statement=boom)  # fmt: skip
        assert digest(path) == before, point
        verify_backup(tmp_path / f"b{n}", digest=before, schema_version=version)
    final = tmp_path / "final.sqlite3"
    _old(final, version)
    migrate_ledger(final, mode=RuntimeMode.OBSERVE, backup_path=tmp_path / "bf",
                   migrations=ALL, now=NOW)  # fmt: skip
    Database.open(final, RuntimeMode.OBSERVE, auto_migrate=False).close()


# ============================================================ backup interruption
def test_killed_backup_leaves_an_unverifiable_copy_and_the_source_intact(e: CliEnv) -> None:
    before = digest(e.ledger)
    target = e.root / "partial.sqlite3"
    proc = mp.get_context("spawn").Process(target=backup_and_die,
                                           args=(str(e.ledger), str(target)))  # fmt: skip
    proc.start()
    proc.join(60)
    assert proc.exitcode == CRASH_EXIT and target.exists()
    with pytest.raises(BackupVerificationError):
        verify_backup(target, digest=before, schema_version=len(ALL))
    assert e.run("verify-ledger", str(target))[0] == 9
    assert digest(e.ledger) == before
    code, env = e.run("backup", str(e.root / "good.sqlite3"))
    assert code == 0 and env["result"]["verified"] is True


# ============================================================ manual restore (D15-8)
def test_manual_restore_procedure(e: CliEnv) -> None:
    e.account.clock.advance(minutes=20)
    assert e.run("reconcile")[0] == 0
    assert e.run("resume", "--reason", "go")[0] == 0
    backup = e.root / "backup.sqlite3"
    assert e.run("backup", str(backup))[0] == 0
    assert e.run("halt", "--reason", "state changes after the backup")[0] == 0
    # 1. verify the backup  2. set the current ledger aside  3. copy, 0600  4. verify
    code, env = e.run("verify-ledger", str(backup))
    assert code == 0 and env["result"]["verified"] and env["result"]["invariants_ok"]
    aside = e.root / "ledger.before-restore.sqlite3"
    shutil.move(e.ledger, aside)
    shutil.copyfile(backup, e.ledger)
    e.ledger.chmod(0o600)
    code, env = e.run("verify-ledger", str(e.ledger))
    assert code == 0 and env["result"]["trading_state"]["halted"] is False
    # 5. bring the restored ledger up to date with the broker before anything else
    e.account.clock.advance(minutes=1)
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN"
    assert aside.exists()  # the replaced ledger is kept, never deleted
