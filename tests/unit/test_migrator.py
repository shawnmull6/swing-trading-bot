"""Step 4: migration packaging, splitting and verification (no files on disk)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from swing_trading_bot.ledger.errors import (
    IncompleteDatabaseError,
    MigrationChecksumError,
    MigrationError,
    UnsupportedSchemaVersionError,
)
from swing_trading_bot.ledger.migrator import (
    Migration,
    applied_migrations,
    apply_pending,
    packaged_migrations,
    validate_sequence,
    verify_applied,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)
M1 = Migration(1, "init", "CREATE TABLE a (x TEXT) STRICT;\n")
M2 = Migration(
    2,
    "more",
    "-- comment line\nCREATE TABLE b (y TEXT) STRICT;\n"
    "CREATE TRIGGER b_guard BEFORE DELETE ON b\nBEGIN SELECT RAISE(ABORT, 'no; really');\nEND;\n",
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", isolation_level=None)
    yield c
    c.close()


def _noop(_c: sqlite3.Connection) -> None:
    return None


def test_packaged_migrations_are_contiguous_and_named() -> None:
    migrations = packaged_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "init"
    assert len(migrations[0].checksum) == 64


def test_packaged_schema_never_uses_real_or_defaults_for_values() -> None:
    code_lines = [
        ln
        for m in packaged_migrations()
        for ln in m.sql.splitlines()
        if not ln.lstrip().startswith("--")
    ]
    sql = "\n".join(code_lines).upper()
    assert re.search(r"\bREAL\b", sql) is None
    assert "DEFAULT" not in sql  # no silent defaults anywhere in 0001
    assert "FLOAT" not in sql


def test_statement_splitting_handles_triggers_and_comments() -> None:
    statements = M2.statements()
    assert len(statements) == 2
    assert statements[1].startswith("CREATE TRIGGER")
    assert statements[1].endswith("END;")


def test_trailing_incomplete_statement_rejected() -> None:
    with pytest.raises(MigrationError, match="trailing"):
        Migration(1, "bad", "CREATE TABLE a (x TEXT);\nCREATE TABLE b (").statements()


@pytest.mark.parametrize("versions", [[2], [1, 3], [1, 1], [0, 1]])
def test_sequence_must_be_contiguous(versions: list[int]) -> None:
    with pytest.raises(MigrationError, match="contiguously"):
        validate_sequence([Migration(v, f"m{v}", "SELECT 1;") for v in versions])


def test_apply_and_repeat_is_noop(conn: sqlite3.Connection) -> None:
    assert apply_pending(conn, [M1], now=NOW, bootstrap=_noop) == [1]
    assert apply_pending(conn, [M1], now=NOW) == []
    assert apply_pending(conn, [M1, M2], now=NOW) == [2]
    assert apply_pending(conn, [M1, M2], now=NOW) == []
    assert [a.version for a in applied_migrations(conn)] == [1, 2]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_fresh_schema_requires_bootstrap(conn: sqlite3.Connection) -> None:
    with pytest.raises(IncompleteDatabaseError, match="bootstrap"):
        apply_pending(conn, [M1], now=NOW)


def test_refuses_inside_open_transaction(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    with pytest.raises(MigrationError, match="open transaction"):
        apply_pending(conn, [M1], now=NOW, bootstrap=_noop)


def test_failed_migration_rolls_back_completely(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1], now=NOW, bootstrap=_noop)
    bad = Migration(2, "bad", "CREATE TABLE c (z TEXT) STRICT;\nINSERT INTO nope VALUES (1);\n")
    with pytest.raises(MigrationError, match="rolled back"):
        apply_pending(conn, [M1, bad], now=NOW)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "c" not in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert [a.version for a in applied_migrations(conn)] == [1]


def test_hook_interruption_rolls_back(conn: sqlite3.Connection) -> None:
    def explode(version: int, index: int) -> None:
        if version == 1 and index == 0:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        apply_pending(conn, [M1], now=NOW, bootstrap=_noop, before_statement=explode)
    assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert not conn.in_transaction


def test_checksum_change_detected(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1], now=NOW, bootstrap=_noop)
    edited = Migration(1, "init", M1.sql + "-- edited\n")
    with pytest.raises(MigrationChecksumError):
        verify_applied(conn, [edited])


def test_newer_schema_refused(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1, M2], now=NOW, bootstrap=_noop)
    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        verify_applied(conn, [M1])


def test_user_version_disagreement_refused(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1], now=NOW, bootstrap=_noop)
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(UnsupportedSchemaVersionError, match="user_version"):
        verify_applied(conn, [M1])


def test_tables_without_history_refused(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE stray (x)")
    with pytest.raises(IncompleteDatabaseError):
        verify_applied(conn, [M1])


def test_history_gap_refused(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1, M2], now=NOW, bootstrap=_noop)
    conn.execute("DROP TRIGGER schema_migrations_no_delete")
    conn.execute("DELETE FROM schema_migrations WHERE version = 1")
    with pytest.raises(UnsupportedSchemaVersionError, match="gaps"):
        verify_applied(conn, [M1, M2])


def test_schema_migrations_is_append_only(conn: sqlite3.Connection) -> None:
    apply_pending(conn, [M1], now=NOW, bootstrap=_noop)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE schema_migrations SET checksum = ?", ("0" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM schema_migrations")
