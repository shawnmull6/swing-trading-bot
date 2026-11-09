"""SQLite connection factory, mode isolation and explicit transactions (ARCHITECTURE §12).

Opening a database
------------------
* :meth:`Database.create` makes a **new** file (refuses if it exists), applies every
  migration and stamps the database identity (mode) and an initial persistent halt in
  the same transaction as migration 0001.
* :meth:`Database.open` opens an **existing** file only (refuses if missing, so a typo
  can never silently produce an empty ledger), verifies the application id, migration
  history and checksums, applies pending migrations atomically, checks the mode and
  runs SQLite integrity and foreign-key checks.
* ``paper_execute`` is not constructible in M1.

Transactions
------------
* :meth:`Database.transaction` issues ``BEGIN IMMEDIATE`` and yields a :class:`Tx`
  (read-write). Commit on normal exit, rollback on any exception.
* :meth:`Database.read` yields a :class:`ReadTx` in a deferred transaction with
  ``PRAGMA query_only`` enabled, always rolled back: a consistent read-only snapshot.
* Nesting either kind raises :class:`NestedTransactionError`. A Tx/ReadTx object is
  unusable after its block ends.
* :func:`assert_no_transaction` lets I/O code (broker adapters, later steps) prove it
  is not running inside a database transaction (no network I/O inside a transaction).
"""

from __future__ import annotations

import contextvars
import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

from swing_trading_bot.domain.enums import HaltReason, RuntimeMode
from swing_trading_bot.ledger.codec import str_from_db, ts_from_db, ts_to_db
from swing_trading_bot.ledger.errors import (
    DatabaseCorruptError,
    DatabaseExistsError,
    DatabaseNotFoundError,
    ForeignDatabaseError,
    IncompleteDatabaseError,
    ModeMismatchError,
    NestedTransactionError,
    PersistenceError,
    ReadOnlyTransactionError,
    TransactionClosedError,
    wrap_sqlite_error,
)
from swing_trading_bot.ledger.migrator import Migration, apply_pending, packaged_migrations

APPLICATION_NAME: Final = "swing-trading-bot"
APPLICATION_ID: Final = 0x53544231  # "STB1"
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000
SYSTEM_ACTOR: Final = "system"

_log = logging.getLogger(__name__)
_ACTIVE_TX: contextvars.ContextVar[bool] = contextvars.ContextVar("db_tx_active", default=False)

Params = Sequence[object]


def assert_no_transaction(operation: str) -> None:
    """Raise if called while a database transaction is open in this context."""
    if _ACTIVE_TX.get():
        raise NestedTransactionError(f"{operation} must not run inside a database transaction")


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    application: str
    mode: RuntimeMode
    created_at: datetime


class Session:
    """Shared query surface for read and write transactions."""

    _writable: bool = False

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn: sqlite3.Connection | None = conn

    def _live(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TransactionClosedError("transaction is closed")
        return self._conn

    def _close(self) -> None:
        self._conn = None

    @property
    def writable(self) -> bool:
        return self._writable

    def fetch_one(self, sql: str, params: Params = ()) -> tuple[Any, ...] | None:
        try:
            row = self._live().execute(sql, tuple(params)).fetchone()
        except sqlite3.Error as exc:
            raise wrap_sqlite_error(exc, "query") from None
        return None if row is None else tuple(row)

    def fetch_all(self, sql: str, params: Params = ()) -> list[tuple[Any, ...]]:
        try:
            rows = self._live().execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise wrap_sqlite_error(exc, "query") from None
        return [tuple(r) for r in rows]

    def scalar(self, sql: str, params: Params = ()) -> object:
        row = self.fetch_one(sql, params)
        return None if row is None else row[0]


class ReadTx(Session):
    """Read-only snapshot. Writes are rejected by ``PRAGMA query_only``."""


class Tx(Session):
    """Read-write transaction opened with ``BEGIN IMMEDIATE``."""

    _writable = True

    def execute(self, sql: str, params: Params = ()) -> int:
        """Execute one write statement; returns the affected row count."""
        try:
            cursor = self._live().execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise wrap_sqlite_error(exc, "write") from None
        return cursor.rowcount

    def insert(self, sql: str, params: Params = ()) -> int:
        """Execute an INSERT and return the new rowid."""
        try:
            cursor = self._live().execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise wrap_sqlite_error(exc, "insert") from None
        rowid = cursor.lastrowid
        if rowid is None:  # pragma: no cover - INSERT always sets lastrowid
            raise PersistenceError("insert did not produce a rowid")
        return rowid


def require_tx(session: Session) -> Tx:
    """Narrow a session to a writable transaction (raises for ReadTx)."""
    if not isinstance(session, Tx):
        raise ReadOnlyTransactionError("write attempted in a read-only transaction")
    return session


class Database:
    """A single connection to one ledger file. Not thread-safe; one writer process."""

    def __init__(self, conn: sqlite3.Connection, path: Path, identity: DatabaseIdentity) -> None:
        self._conn: sqlite3.Connection | None = conn
        self.path = path
        self.identity = identity
        self._in_tx = False

    # ------------------------------------------------------------------ factory
    @classmethod
    def create(
        cls,
        path: Path,
        mode: RuntimeMode,
        *,
        now: datetime | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        migrations: Sequence[Migration] | None = None,
    ) -> Self:
        _refuse_unsupported_mode(mode)
        if path.exists():
            raise DatabaseExistsError(f"database already exists: {path}")
        if not path.parent.is_dir():
            raise DatabaseNotFoundError(f"directory does not exist: {path.parent}")
        created_at = now if now is not None else utc_now()
        # Create the file owner-read/write only before SQLite touches it.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = _connect(path, busy_timeout_ms)
        try:

            def bootstrap(c: sqlite3.Connection) -> None:
                c.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                c.execute(
                    "INSERT INTO database_identity (id, application, mode, created_at) "
                    "VALUES (1, ?, ?, ?)",
                    (APPLICATION_NAME, mode.value, ts_to_db(created_at)),
                )
                c.execute(
                    "INSERT INTO trading_state "
                    "(id, halted, reason_code, reason_detail, changed_at, changed_by) "
                    "VALUES (1, 1, ?, ?, ?, ?)",
                    (
                        HaltReason.INITIALIZED.value,
                        "new database; operator resume required",
                        ts_to_db(created_at),
                        SYSTEM_ACTOR,
                    ),
                )
                c.execute(
                    "INSERT INTO halt_events (ts, action, reason_code, detail, actor) "
                    "VALUES (?, 'HALT', ?, ?, ?)",
                    (
                        ts_to_db(created_at),
                        HaltReason.INITIALIZED.value,
                        "database created",
                        SYSTEM_ACTOR,
                    ),
                )

            apply_pending(
                conn,
                migrations if migrations is not None else packaged_migrations(),
                now=created_at,
                bootstrap=bootstrap,
            )
            db = cls._finish_open(conn, path, mode)
        except BaseException:
            conn.close()
            raise
        _log.info("database created", extra={"db_path": str(path), "mode": mode.value})
        return db

    @classmethod
    def open(
        cls,
        path: Path,
        mode: RuntimeMode,
        *,
        now: datetime | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        migrations: Sequence[Migration] | None = None,
    ) -> Self:
        _refuse_unsupported_mode(mode)
        if not path.is_file():
            raise DatabaseNotFoundError(f"database not found: {path}")
        conn = _connect(path, busy_timeout_ms)
        try:
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            if app_id == 0:
                raise IncompleteDatabaseError(
                    f"{path} carries no application id: not a ledger, or creation was interrupted"
                )
            if app_id != APPLICATION_ID:
                raise ForeignDatabaseError(f"{path} was not created by {APPLICATION_NAME}")
            apply_pending(
                conn,
                migrations if migrations is not None else packaged_migrations(),
                now=now if now is not None else utc_now(),
                bootstrap=None,
            )
            db = cls._finish_open(conn, path, mode)
        except sqlite3.Error as exc:
            conn.close()
            raise wrap_sqlite_error(exc, "open") from None
        except BaseException:
            conn.close()
            raise
        return db

    @classmethod
    def _finish_open(cls, conn: sqlite3.Connection, path: Path, mode: RuntimeMode) -> Self:
        row = conn.execute(
            "SELECT application, mode, created_at FROM database_identity WHERE id = 1"
        ).fetchone()
        if row is None:
            raise IncompleteDatabaseError("database identity row is missing")
        identity = DatabaseIdentity(
            application=str_from_db(row[0]),
            mode=RuntimeMode(str_from_db(row[1])),
            created_at=ts_from_db(row[2]),
        )
        if identity.mode is not mode:
            raise ModeMismatchError(
                f"database {path} is a '{identity.mode.value}' ledger; refusing to open it in "
                f"'{mode.value}' mode"
            )
        _verify_physical_integrity(conn)
        if conn.execute("SELECT 1 FROM trading_state WHERE id = 1").fetchone() is None:
            raise IncompleteDatabaseError("trading_state row is missing")
        return cls(conn, path, identity)

    # ------------------------------------------------------------- transactions
    def _live(self) -> sqlite3.Connection:
        if self._conn is None:
            raise PersistenceError("database is closed")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        conn = self._enter()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self._exit()
            raise wrap_sqlite_error(exc, "begin") from None
        tx = Tx(conn)
        try:
            yield tx
        except BaseException:
            tx._close()
            self._rollback(conn)
            self._exit()
            raise
        tx._close()
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise wrap_sqlite_error(exc, "commit") from None
        finally:
            self._exit()

    @contextmanager
    def read(self) -> Iterator[ReadTx]:
        conn = self._enter()
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
        except sqlite3.Error as exc:
            conn.execute("PRAGMA query_only = OFF")
            self._exit()
            raise wrap_sqlite_error(exc, "begin read") from None
        session = ReadTx(conn)
        try:
            yield session
        finally:
            session._close()
            self._rollback(conn)
            conn.execute("PRAGMA query_only = OFF")
            self._exit()

    def _enter(self) -> sqlite3.Connection:
        conn = self._live()
        if self._in_tx or _ACTIVE_TX.get() or conn.in_transaction:
            raise NestedTransactionError("a database transaction is already open")
        self._in_tx = True
        _ACTIVE_TX.set(True)
        return conn

    def _exit(self) -> None:
        self._in_tx = False
        _ACTIVE_TX.set(False)

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback failure is logged only
                _log.exception("rollback failed")

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    # ------------------------------------------------------------------ status
    def pragma(self, name: str) -> object:
        if not name.isidentifier():
            raise ValueError("invalid pragma name")
        return self._live().execute(f"PRAGMA {name}").fetchone()[0]

    def close(self) -> None:
        if self._conn is not None:
            if self._in_tx:
                raise NestedTransactionError("cannot close while a transaction is open")
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _refuse_unsupported_mode(mode: RuntimeMode) -> None:
    if mode is RuntimeMode.PAPER_EXECUTE:
        raise ModeMismatchError("paper_execute mode is not available in M1")


def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")
    try:
        conn = sqlite3.connect(
            path,
            isolation_level=None,  # explicit BEGIN/COMMIT only
            detect_types=0,
            check_same_thread=True,
            cached_statements=256,
        )
    except sqlite3.Error as exc:
        raise wrap_sqlite_error(exc, "connect") from None
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise PersistenceError(f"could not enable WAL journal mode (got {mode!r})")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA cell_size_check = ON")
        if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise PersistenceError("foreign key enforcement unavailable")  # pragma: no cover
        if int(conn.execute("PRAGMA synchronous").fetchone()[0]) != 2:  # synchronous=FULL
            raise PersistenceError("synchronous=FULL unavailable")  # pragma: no cover
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise wrap_sqlite_error(exc, "connect") from None
    except BaseException:
        conn.close()
        raise
    return conn


def _verify_physical_integrity(conn: sqlite3.Connection) -> None:
    try:
        result = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptError(f"integrity check failed: {exc.args[0]}") from None
    if result != ["ok"]:
        raise DatabaseCorruptError(f"integrity check failed: {'; '.join(result[:5])}")
    if fk_violations:
        raise DatabaseCorruptError(f"{len(fk_violations)} foreign key violation(s) present")
