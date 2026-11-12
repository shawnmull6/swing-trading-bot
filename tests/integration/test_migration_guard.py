"""Step 14: explicit, backed-up migrations; observe ledgers never migrate implicitly."""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.app.lock import LedgerLock, LockMode
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger import backup as backup_module
from swing_trading_bot.ledger.backup import BackupVerificationError, content_digest
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import (
    MigrationChecksumError,
    MigrationError,
    MigrationRequiredError,
    ModeMismatchError,
    UnsupportedSchemaVersionError,
)
from swing_trading_bot.ledger.migration_guard import migrate_ledger, plan_migration
from swing_trading_bot.ledger.migrator import Migration, packaged_migrations
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from tests.support.cli_env import CliEnv, cli_env

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
ALL = packaged_migrations()
OLD = ALL[:-1]


def old_ledger(path: Path, mode: RuntimeMode = RuntimeMode.OBSERVE) -> Path:
    """A ledger one schema version behind, with some data in it."""
    db = Database.create(path, mode, now=NOW, migrations=OLD)
    with db.transaction() as tx:
        VirtualPortfolio.initialize(tx, name="main", mode=mode, initial_capital=Decimal(5000),
                                    max_capital_allocation=Decimal(5000),
                                    account_profile="cash_conservative", at=NOW)  # fmt: skip
    db.close()
    return path


def digest(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return content_digest(conn)
    finally:
        conn.close()


def version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    old_ledger(env.ledger)
    return env


# ============================================================ implicit migration refused
def test_observe_ledger_is_never_migrated_on_open(e: CliEnv) -> None:
    before = digest(e.ledger)
    with pytest.raises(MigrationRequiredError):
        Database.open(e.ledger, RuntimeMode.OBSERVE)  # even with the default auto_migrate
    for argv in (("status",), ("halts",), ("sync",), ("reconcile",), ("audit",)):
        code, env = e.run(*argv)
        assert code == 9 and env["result"]["error"] == "MigrationRequiredError", argv
    assert digest(e.ledger) == before and version(e.ledger) == len(OLD)


def test_simulation_ledgers_migrate_only_when_allowed(tmp_path: Path) -> None:
    path = old_ledger(tmp_path / "sim.sqlite3", RuntimeMode.SIMULATE)
    with pytest.raises(MigrationRequiredError):
        Database.open(path, RuntimeMode.SIMULATE, auto_migrate=False)
    Database.open(path, RuntimeMode.SIMULATE).close()  # existing auto-migration kept
    assert version(path) == len(ALL)


def test_mode_is_checked_before_any_migration(e: CliEnv) -> None:
    before = digest(e.ledger)
    with pytest.raises(ModeMismatchError):
        Database.open(e.ledger, RuntimeMode.SIMULATE)
    assert digest(e.ledger) == before


# ============================================================ explicit migration
def test_migrate_requires_a_backup_path(e: CliEnv) -> None:
    code, _ = e.run("migrate")
    assert code == 2


def test_migrate_backs_up_verifies_and_migrates(e: CliEnv, tmp_path: Path) -> None:
    before = digest(e.ledger)
    target = tmp_path / "pre-migration.sqlite3"
    code, env = e.run("migrate", "--backup", str(target))
    assert code == 0, env
    result = env["result"]
    assert (result["from_version"], result["to_version"]) == (len(OLD), len(ALL))
    assert result["backup"]["verified"] is True
    assert digest(target) == before  # the backup is the pre-migration ledger, exactly
    assert target.stat().st_mode & 0o777 == 0o600
    assert version(e.ledger) == len(ALL)
    code, env = e.run("halts")
    assert code == 0 and env["result"]["trading_state"]["reason_code"] == "INITIALIZED"
    code, env = e.run("migrate", "--backup", str(tmp_path / "unused.sqlite3"))
    assert code == 0 and env["result"]["applied"] == [] and env["result"]["backup"] is None
    assert not (tmp_path / "unused.sqlite3").exists()


def test_existing_backup_destination_is_refused(e: CliEnv, tmp_path: Path) -> None:
    target = tmp_path / "taken.sqlite3"
    target.write_text("keep me")
    before = digest(e.ledger)
    code, env = e.run("migrate", "--backup", str(target))
    assert code == 9 and "already exists" in env["result"]["detail"]
    assert target.read_text() == "keep me" and digest(e.ledger) == before


def test_failed_migration_leaves_original_untouched(e: CliEnv, tmp_path: Path) -> None:
    before = digest(e.ledger)

    def boom(v: int, i: int) -> None:
        if v == len(ALL) and i == 1:
            raise RuntimeError("injected failure mid-migration")

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_ledger(e.ledger, mode=RuntimeMode.OBSERVE, backup_path=tmp_path / "b.sqlite3",
                       migrations=ALL, now=NOW, before_statement=boom)  # fmt: skip
    assert digest(e.ledger) == before and version(e.ledger) == len(OLD)
    assert digest(tmp_path / "b.sqlite3") == before  # verified backup still there


def _killed_migration(ledger: str, backup: str) -> None:
    def die(v: int, i: int) -> None:
        if v == len(ALL) and i == 1:
            os._exit(71)

    with LedgerLock(Path(ledger), LockMode.EXCLUSIVE, command="migrate"):
        migrate_ledger(Path(ledger), mode=RuntimeMode.OBSERVE, backup_path=Path(backup),
                       migrations=ALL, now=NOW, before_statement=die)  # fmt: skip


def test_process_killed_mid_migration(e: CliEnv, tmp_path: Path) -> None:
    before = digest(e.ledger)
    proc = mp.get_context("spawn").Process(
        target=_killed_migration, args=(str(e.ledger), str(tmp_path / "b.sqlite3"))
    )
    proc.start()
    proc.join(60)
    assert proc.exitcode == 71
    assert digest(e.ledger) == before and version(e.ledger) == len(OLD)  # rolled back
    with LedgerLock(e.ledger, LockMode.EXCLUSIVE, command="test"):  # the kernel freed it
        pass
    code, _ = e.run("migrate", "--backup", str(tmp_path / "b2.sqlite3"))
    assert code == 0 and version(e.ledger) == len(ALL)


def test_invalid_backup_aborts_migration(
    e: CliEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt_copy(_source: sqlite3.Connection, destination: Path) -> None:
        destination.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    monkeypatch.setattr(backup_module, "_copy", corrupt_copy)
    before = digest(e.ledger)
    with pytest.raises(BackupVerificationError):
        migrate_ledger(e.ledger, mode=RuntimeMode.OBSERVE, backup_path=tmp_path / "b.sqlite3",
                       migrations=ALL, now=NOW)  # fmt: skip
    assert digest(e.ledger) == before and version(e.ledger) == len(OLD)


def test_backup_with_divergent_content_is_rejected(
    e: CliEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = backup_module._copy

    def tampering_copy(source: sqlite3.Connection, destination: Path) -> None:
        original(source, destination)
        conn = sqlite3.connect(destination)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()

    monkeypatch.setattr(backup_module, "_copy", tampering_copy)
    code, env = e.run("migrate", "--backup", str(tmp_path / "b.sqlite3"))
    assert code == 9 and env["result"]["error"] == "BackupVerificationError"
    assert version(e.ledger) == len(OLD)


def test_schema_newer_than_application_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite3"
    Database.create(path, RuntimeMode.OBSERVE, now=NOW).close()
    with pytest.raises(UnsupportedSchemaVersionError):
        plan_migration(path, RuntimeMode.OBSERVE, OLD)


def test_changed_migration_checksum_is_refused(e: CliEnv, tmp_path: Path) -> None:
    tampered = (*OLD[:-1], Migration(OLD[-1].version, OLD[-1].name, OLD[-1].sql + "\n-- x\n"))
    with pytest.raises(MigrationChecksumError):
        migrate_ledger(e.ledger, mode=RuntimeMode.OBSERVE, backup_path=tmp_path / "b.sqlite3",
                       migrations=(*tampered, ALL[-1]), now=NOW)  # fmt: skip
    assert not (tmp_path / "b.sqlite3").exists()  # verification precedes the backup


def test_migrate_refused_while_ledger_in_use(e: CliEnv, tmp_path: Path) -> None:
    with LedgerLock(e.ledger, LockMode.SHARED, command="reader"):
        code, _ = e.run("migrate", "--backup", str(tmp_path / "b.sqlite3"))
    assert code == 6
    assert version(e.ledger) == len(OLD) and not (tmp_path / "b.sqlite3").exists()


# ============================================================ backup command
def test_backup_command_writes_verified_copy(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.init()
    target = tmp_path / "backup.sqlite3"
    code, env = e.run("backup", str(target))
    assert code == 0 and env["result"]["verified"] is True
    assert digest(target) == digest(e.ledger)
    with Database.open(target, RuntimeMode.OBSERVE, auto_migrate=False) as copy:
        assert copy.identity.mode is RuntimeMode.OBSERVE
    code, env = e.run("backup", str(target))
    assert code == 9 and "already exists" in env["result"]["detail"]
