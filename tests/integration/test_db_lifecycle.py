"""Step 4: creating, reopening, migrating and validating ledger databases."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import HaltReason, RuntimeMode
from swing_trading_bot.ledger.db import APPLICATION_ID, Database, ReadTx
from swing_trading_bot.ledger.errors import (
    DatabaseCorruptError,
    DatabaseExistsError,
    DatabaseNotFoundError,
    ForeignDatabaseError,
    IncompleteDatabaseError,
    MigrationChecksumError,
    MigrationError,
    ModeMismatchError,
    UnsupportedSchemaVersionError,
)
from swing_trading_bot.ledger.integrity import IntegrityReport, integrity_report
from swing_trading_bot.ledger.migrator import Migration, packaged_migrations
from swing_trading_bot.ledger.repositories import SyncStateRepository, TradingStateRepository

from .conftest import T0, at

N = len(packaged_migrations())

EXPECTED_TABLES = {
    "calendar_fetches",
    "settlement_days",
    "session_closes",
    "settlement_resolutions",
    "schema_migrations",
    "database_identity",
    "portfolios",
    "rebaseline_requests",
    "broker_baselines",
    "ledger_adjustments",
    "orders",
    "order_resolution_attempts",
    "cash_reservations",
    "broker_activities",
    "fills",
    "cash_entries",
    "positions",
    "price_marks",
    "market_calendar",
    "equity_snapshots",
    "loss_limit_events",
    "order_events",
    "trading_state",
    "halt_events",
    "sync_state",
    "reconciliation_runs",
    "reconciliation_findings",
    "rebaseline_events",
    "rebaseline_acknowledgements",
}


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


# ------------------------------------------------------------------- creation


def test_fresh_database_has_complete_schema_and_settings(db: Database, db_path: Path) -> None:
    with db.read() as s:
        tables = {
            str(r[0])
            for r in s.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        strict = {
            str(r[0]) for r in s.fetch_all("SELECT name FROM pragma_table_list WHERE strict = 1")
        }
        report = integrity_report(s, full=True)
    assert tables == EXPECTED_TABLES
    assert strict == EXPECTED_TABLES
    assert report.ok, report.problems()
    assert db.pragma("journal_mode") == "wal"
    assert db.pragma("foreign_keys") == 1
    assert db.pragma("synchronous") == 2  # FULL
    assert db.pragma("busy_timeout") == 5000
    assert db.pragma("application_id") == APPLICATION_ID
    assert db.pragma("user_version") == len(packaged_migrations())
    assert db.identity.mode is RuntimeMode.SIMULATE
    assert db.identity.created_at == T0
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600


def test_new_database_starts_persistently_halted(db: Database) -> None:
    with db.read() as s:
        state = TradingStateRepository().get(s)
        events = TradingStateRepository().events(s)
    assert state.halted is True
    assert state.reason_code == HaltReason.INITIALIZED.value
    assert [(e.action, e.reason_code) for e in events] == [("HALT", "INITIALIZED")]


def test_no_portfolio_or_ledger_money_is_created_by_step_4(db: Database) -> None:
    with db.read() as s:
        assert s.scalar("SELECT count(*) FROM portfolios") == 0
        assert s.scalar("SELECT count(*) FROM cash_entries") == 0
        assert s.scalar("SELECT count(*) FROM broker_baselines") == 0


def test_create_refuses_existing_file(db: Database, db_path: Path) -> None:
    with pytest.raises(DatabaseExistsError):
        Database.create(db_path, RuntimeMode.SIMULATE)


def test_create_refuses_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(DatabaseNotFoundError, match="directory"):
        Database.create(tmp_path / "nope" / "x.sqlite3", RuntimeMode.SIMULATE)


def test_open_refuses_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DatabaseNotFoundError):
        Database.open(tmp_path / "missing.sqlite3", RuntimeMode.OBSERVE)
    assert not (tmp_path / "missing.sqlite3").exists()  # never created implicitly


def test_failed_creation_leaves_no_schema(tmp_path: Path) -> None:
    path = tmp_path / "x.sqlite3"
    broken = (Migration(1, "init", "CREATE TABLE a (x TEXT) STRICT;\nSELECT * FROM nope;\n"),)
    with pytest.raises(MigrationError):
        Database.create(path, RuntimeMode.SIMULATE, migrations=broken)
    with pytest.raises(IncompleteDatabaseError):
        Database.open(path, RuntimeMode.SIMULATE)


# ---------------------------------------------------------------------- reopen


def test_reopen_preserves_state(db: Database, db_path: Path) -> None:
    with db.transaction() as tx:
        SyncStateRepository().set(tx, "activities.cursor_id", "abc::1", at(1))
    db.close()
    with Database.open(db_path, RuntimeMode.SIMULATE, now=at(5)) as again:
        with again.read() as s:
            assert SyncStateRepository().get(s, "activities.cursor_id") == "abc::1"
            assert TradingStateRepository().get(s).halted is True
            assert s.scalar("SELECT count(*) FROM schema_migrations") == N
        assert again.identity.created_at == T0


def test_repeated_open_does_not_reapply_migrations(db: Database, db_path: Path) -> None:
    db.close()
    for i in range(3):
        with Database.open(db_path, RuntimeMode.SIMULATE, now=at(i)) as again, again.read() as s:
            rows = s.fetch_all("SELECT version, applied_at FROM schema_migrations")
            assert rows == [(v, "2026-11-09T15:30:00.000000+00:00") for v in range(1, N + 1)]


def test_pending_migration_applied_on_open(db: Database, db_path: Path) -> None:
    db.close()
    extra = Migration(N + 1, "extra", "CREATE TABLE extra_table (x TEXT NOT NULL) STRICT;\n")
    migrations = (*packaged_migrations(), extra)
    with Database.open(db_path, RuntimeMode.SIMULATE, migrations=migrations) as again:
        assert again.pragma("user_version") == N + 1
    # The default (packaged-only) application now sees a newer schema and refuses.
    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        Database.open(db_path, RuntimeMode.SIMULATE)


def test_failed_pending_migration_keeps_previous_version(db: Database, db_path: Path) -> None:
    db.close()
    bad = Migration(
        N + 1, "bad", "CREATE TABLE t2 (x TEXT) STRICT;\nINSERT INTO nope VALUES (1);\n"
    )
    with pytest.raises(MigrationError, match="rolled back"):
        Database.open(db_path, RuntimeMode.SIMULATE, migrations=(*packaged_migrations(), bad))
    with Database.open(db_path, RuntimeMode.SIMULATE) as again, again.read() as s:
        assert again.pragma("user_version") == N
        assert s.scalar("SELECT count(*) FROM sqlite_master WHERE name = 't2'") == 0


def test_interrupted_migration_process_kill_is_atomic(tmp_path: Path, db: Database) -> None:
    """A process killed mid-migration (os._exit, no cleanup) leaves the old version."""
    path = db.path
    db.close()
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from swing_trading_bot.domain.enums import RuntimeMode
        from swing_trading_bot.ledger import migrator
        from swing_trading_bot.ledger.db import Database
        extra = migrator.Migration(
            {N + 1}, "extra",
            "CREATE TABLE k1 (x TEXT) STRICT;\\nCREATE TABLE k2 (x TEXT) STRICT;\\n"
            "CREATE TABLE k3 (x TEXT) STRICT;\\n",
        )
        original = migrator.apply_pending
        def killing(conn, migrations, **kw):
            def hook(version, index):
                if version == {N + 1} and index == 2:
                    os._exit(17)
            return original(conn, migrations, **kw, before_statement=hook)
        import swing_trading_bot.ledger.db as dbmod
        dbmod.apply_pending = killing
        Database.open(Path({str(path)!r}), RuntimeMode.SIMULATE,
                      migrations=(*migrator.packaged_migrations(), extra))
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", script], capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 17, result.stderr.decode()
    with Database.open(path, RuntimeMode.SIMULATE) as again, again.read() as s:
        assert again.pragma("user_version") == N
        names = {str(r[0]) for r in s.fetch_all("SELECT name FROM sqlite_master")}
        assert not {"k1", "k2", "k3"} & names
        assert integrity_report(s, full=True).ok


def test_interrupted_creation_is_not_a_usable_database(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.sqlite3"
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from swing_trading_bot.domain.enums import RuntimeMode
        from swing_trading_bot.ledger import migrator
        import swing_trading_bot.ledger.db as dbmod
        original = migrator.apply_pending
        def killing(conn, migrations, **kw):
            def hook(version, index):
                if index == 20:
                    os._exit(23)
            return original(conn, migrations, **kw, before_statement=hook)
        dbmod.apply_pending = killing
        dbmod.Database.create(Path({str(path)!r}), RuntimeMode.SIMULATE)
        """
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 23, result.stderr.decode()
    with pytest.raises(IncompleteDatabaseError):
        Database.open(path, RuntimeMode.SIMULATE)


# ----------------------------------------------------------------------- modes


@pytest.mark.parametrize("other", [RuntimeMode.OBSERVE])
def test_mode_mismatch_rejected(db: Database, db_path: Path, other: RuntimeMode) -> None:
    db.close()
    with pytest.raises(ModeMismatchError, match="'simulate' ledger"):
        Database.open(db_path, other)


def test_observe_database_cannot_be_opened_for_simulation(tmp_path: Path) -> None:
    path = tmp_path / "observe.sqlite3"
    Database.create(path, RuntimeMode.OBSERVE).close()
    with pytest.raises(ModeMismatchError):
        Database.open(path, RuntimeMode.SIMULATE)
    with Database.open(path, RuntimeMode.OBSERVE) as ok:
        assert ok.identity.mode is RuntimeMode.OBSERVE


def test_paper_execute_is_not_available_in_m1(tmp_path: Path, db: Database) -> None:
    with pytest.raises(ModeMismatchError, match="not available"):
        Database.create(tmp_path / "pe.sqlite3", RuntimeMode.PAPER_EXECUTE)
    with pytest.raises(ModeMismatchError, match="not available"):
        Database.open(db.path, RuntimeMode.PAPER_EXECUTE)
    assert not (tmp_path / "pe.sqlite3").exists()


def test_identity_is_immutable(db: Database) -> None:
    db.close()
    conn = _raw(db.path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE database_identity SET mode = 'observe'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM database_identity")
    conn.close()


# --------------------------------------------------------- corrupt / foreign


def test_garbage_file_is_reported_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "garbage.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all" * 200)
    with pytest.raises(DatabaseCorruptError):
        Database.open(path, RuntimeMode.SIMULATE)


def test_damaged_pages_are_reported_corrupt(db: Database) -> None:
    path = db.path
    db.close()
    data = bytearray(path.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    assert len(data) > 4 * page_size
    for offset in range(page_size * 2, len(data) - 100, page_size):
        data[offset + 8 : offset + 64] = os.urandom(56)
    path.write_bytes(bytes(data))
    with pytest.raises(DatabaseCorruptError):
        Database.open(path, RuntimeMode.SIMULATE)


def test_foreign_sqlite_database_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.sqlite3"
    conn = _raw(path)
    conn.execute("PRAGMA application_id = 12345")
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    with pytest.raises(ForeignDatabaseError):
        Database.open(path, RuntimeMode.SIMULATE)


def test_plain_sqlite_database_refused(tmp_path: Path) -> None:
    path = tmp_path / "plain.sqlite3"
    conn = _raw(path)
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    with pytest.raises(IncompleteDatabaseError):
        Database.open(path, RuntimeMode.SIMULATE)


def test_unsupported_schema_version_refused(db: Database) -> None:
    path = db.path
    db.close()
    conn = _raw(path)
    conn.execute("DROP TRIGGER schema_migrations_no_update")
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?, 'future', ?, '2030-01-01T00:00:00.000000+00:00')",
        (N + 1, "f" * 64),
    )
    conn.execute(f"PRAGMA user_version = {N + 1}")
    conn.close()
    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        Database.open(path, RuntimeMode.SIMULATE)


def test_edited_migration_history_refused(db: Database) -> None:
    path = db.path
    db.close()
    conn = _raw(path)
    conn.execute("DROP TRIGGER schema_migrations_no_update")
    conn.execute("UPDATE schema_migrations SET checksum = ?", ("0" * 64,))
    conn.close()
    with pytest.raises(MigrationChecksumError):
        Database.open(path, RuntimeMode.SIMULATE)


def test_missing_trading_state_refused(db: Database) -> None:
    path = db.path
    db.close()
    conn = _raw(path)
    conn.execute("DROP TRIGGER trading_state_no_delete")
    conn.execute("DELETE FROM trading_state")
    conn.close()
    with pytest.raises(IncompleteDatabaseError, match="trading_state"):
        Database.open(path, RuntimeMode.SIMULATE)


def test_foreign_key_violation_on_disk_refused(db: Database, portfolio_id: int) -> None:
    path = db.path
    db.close()
    conn = _raw(path)  # foreign_keys is OFF on a raw connection: simulate external damage
    conn.execute(
        "INSERT INTO order_events (client_order_id, ts, source) VALUES "
        "('stb-missing', '2026-11-09T15:30:00.000000+00:00', 'LOCAL')"
    )
    conn.close()
    with pytest.raises(DatabaseCorruptError, match="foreign key"):
        Database.open(path, RuntimeMode.SIMULATE)
    assert portfolio_id == 1


def test_integrity_report_flags_problems(db: Database) -> None:
    path = db.path
    db.close()
    conn = _raw(path)
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    conn = sqlite3.connect(path, isolation_level=None)
    report = integrity_report(ReadTx(conn))
    conn.close()
    assert not report.ok
    assert any("schema version" in p for p in report.problems())


def test_integrity_report_lists_every_problem() -> None:
    report = IntegrityReport(
        sqlite_check=("row 3 missing from index",),
        foreign_key_violations=2,
        application_id_ok=False,
        schema_version=0,
        expected_schema_version=1,
        identity_present=False,
        trading_state_present=False,
    )
    assert not report.ok
    assert report.problems() == [
        "sqlite: row 3 missing from index",
        "2 foreign key violation(s)",
        "application id mismatch",
        "schema version 0 != 1",
        "database_identity row missing",
        "trading_state row missing",
    ]
