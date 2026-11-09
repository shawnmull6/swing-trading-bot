"""Step 4: explicit transactions — commit, rollback, nesting, read-only, locking."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database, Tx, assert_no_transaction, require_tx
from swing_trading_bot.ledger.errors import (
    NestedTransactionError,
    PersistenceError,
    ReadOnlyTransactionError,
    TransactionClosedError,
)
from swing_trading_bot.ledger.repositories import SyncStateRepository

from .conftest import at

repo = SyncStateRepository()


class InjectedError(Exception):
    pass


def _value(db: Database, key: str) -> str | None:
    with db.read() as s:
        return repo.get(s, key)


def test_commit_on_success(db: Database) -> None:
    with db.transaction() as tx:
        repo.set(tx, "k", "v1", at())
    assert _value(db, "k") == "v1"


def test_rollback_on_injected_exception(db: Database) -> None:
    with db.transaction() as tx:
        repo.set(tx, "k", "before", at())
    with pytest.raises(InjectedError), db.transaction() as tx:
        repo.set(tx, "k", "after", at(1))
        repo.set(tx, "other", "x", at(1))
        raise InjectedError
    assert _value(db, "k") == "before"
    assert _value(db, "other") is None
    assert not db.in_transaction


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_rollback_on_base_exceptions(db: Database, exc_type: type[BaseException]) -> None:
    with pytest.raises(exc_type), db.transaction() as tx:
        repo.set(tx, "k", "v", at())
        raise exc_type
    assert _value(db, "k") is None


def test_rollback_after_constraint_failure_mid_transaction(db: Database) -> None:
    with pytest.raises(PersistenceError), db.transaction() as tx:
        repo.set(tx, "k", "v", at())
        tx.execute(
            "INSERT INTO trading_state (id, halted, changed_at, changed_by) VALUES (2, 0, 'x', 'y')"
        )
    assert _value(db, "k") is None


def test_nested_write_transaction_rejected(db: Database) -> None:
    with db.transaction() as tx:
        repo.set(tx, "k", "outer", at())
        with pytest.raises(NestedTransactionError), db.transaction():
            pass  # pragma: no cover
        repo.set(tx, "k", "outer2", at())
    assert _value(db, "k") == "outer2"  # outer transaction unaffected


def test_nested_read_inside_write_rejected(db: Database) -> None:
    with db.transaction(), pytest.raises(NestedTransactionError), db.read():
        pass  # pragma: no cover


def test_nested_across_databases_rejected(db: Database, tmp_path: Path) -> None:
    other = Database.create(tmp_path / "other.sqlite3", RuntimeMode.SIMULATE)
    try:
        with db.transaction(), pytest.raises(NestedTransactionError), other.transaction():
            pass  # pragma: no cover
    finally:
        other.close()


def test_transaction_object_unusable_after_block(db: Database) -> None:
    with db.transaction() as tx:
        repo.set(tx, "k", "v", at())
    with pytest.raises(TransactionClosedError):
        repo.set(tx, "k", "late", at())
    with db.read() as s:
        pass
    with pytest.raises(TransactionClosedError):
        s.fetch_one("SELECT 1")


def test_read_snapshot_rejects_writes(db: Database) -> None:
    with db.read() as s:
        with pytest.raises(ReadOnlyTransactionError):
            require_tx(s)
        # Even bypassing the type system, query_only blocks writes at the SQLite level.
        with pytest.raises(PersistenceError, match=r"readonly|read-only|query_only"):
            Tx(s._live()).execute(
                "INSERT INTO sync_state VALUES ('x', 'y', '2026-11-09T15:30:00.000000+00:00')"
            )
    with db.transaction() as tx:  # query_only was reset afterwards
        repo.set(tx, "x", "y", at())


def test_assert_no_transaction_guard(db: Database) -> None:
    assert_no_transaction("broker call")
    with db.transaction(), pytest.raises(NestedTransactionError, match="broker call"):
        assert_no_transaction("broker call")
    assert_no_transaction("broker call")


def test_second_writer_waits_then_fails_with_busy(db_path: Path, db: Database) -> None:
    with db.transaction() as tx:
        repo.set(tx, "k", "v", at())
        contender = sqlite3.connect(db_path, isolation_level=None, timeout=0.2)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute("BEGIN IMMEDIATE")
        contender.close()
    assert _value(db, "k") == "v"


def test_closed_database_rejects_use(db: Database) -> None:
    db.close()
    with pytest.raises(PersistenceError, match="closed"), db.transaction():
        pass  # pragma: no cover
    db.close()  # idempotent


def test_cannot_close_inside_transaction(db: Database) -> None:
    with db.transaction(), pytest.raises(NestedTransactionError):
        db.close()


def test_invalid_pragma_name(db: Database) -> None:
    with pytest.raises(ValueError, match="pragma"):
        db.pragma("user_version; DROP TABLE orders")
