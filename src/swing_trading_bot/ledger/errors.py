"""Persistence error taxonomy.

Messages never contain bound parameter values. SQLite's own error text names
constraints/columns but not values, and :func:`wrap_sqlite_error` never appends
parameters, so secrets or payloads written to the database cannot leak through
exception messages or logs.
"""

from __future__ import annotations

import sqlite3


class PersistenceError(RuntimeError):
    """Base class for every persistence failure."""


class DatabaseNotFoundError(PersistenceError):
    pass


class DatabaseExistsError(PersistenceError):
    pass


class DatabaseCorruptError(PersistenceError):
    pass


class ForeignDatabaseError(PersistenceError):
    """The file is a SQLite database that this application did not create."""


class IncompleteDatabaseError(PersistenceError):
    """The file carries no completed schema (e.g. creation was interrupted)."""


class UnsupportedSchemaVersionError(PersistenceError):
    pass


class MigrationChecksumError(PersistenceError):
    pass


class MigrationError(PersistenceError):
    pass


class MigrationRequiredError(PersistenceError):
    """The ledger has pending migrations that may not be applied implicitly.

    Account-backed (observe) ledgers are migrated only by the explicit, backed-up
    ``migrate`` workflow (``ledger/migration_guard.py``); inspection opens never migrate."""


class ModeMismatchError(PersistenceError):
    pass


class NestedTransactionError(PersistenceError):
    pass


class TransactionClosedError(PersistenceError):
    pass


class ReadOnlyTransactionError(PersistenceError):
    pass


class ConstraintViolationError(PersistenceError):
    """A schema constraint or trigger rejected a write."""


class ConcurrencyConflictError(PersistenceError):
    """An optimistic update found the row in an unexpected state."""


class RecordNotFoundError(PersistenceError):
    pass


def wrap_sqlite_error(exc: sqlite3.Error, operation: str) -> PersistenceError:
    """Translate a sqlite3 error into a PersistenceError without parameter values."""
    detail = str(exc.args[0]) if exc.args else type(exc).__name__
    message = f"{operation}: {detail}"
    if isinstance(exc, sqlite3.IntegrityError):
        return ConstraintViolationError(message)
    if isinstance(exc, sqlite3.DatabaseError) and (
        "malformed" in detail or "not a database" in detail
    ):
        return DatabaseCorruptError(message)
    return PersistenceError(message)
