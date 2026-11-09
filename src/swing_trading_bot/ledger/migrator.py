"""Versioned, checksummed, atomic schema migrations.

Design
------
* Migrations are packaged SQL files ``NNNN_name.sql`` numbered contiguously from 1.
* Each migration runs in **one** ``BEGIN IMMEDIATE`` transaction together with its
  ``schema_migrations`` row and ``PRAGMA user_version`` (both transactional in
  SQLite). An interruption at any point — exception, crash, power loss — leaves the
  database exactly at the previous version.
* Statements are executed one at a time (never ``executescript``, which would issue
  an implicit COMMIT and break atomicity). Splitting uses
  :func:`sqlite3.complete_statement`, so trigger bodies are handled correctly.
* Applied migrations are verified on every open: unknown versions (a newer schema),
  gaps, ``user_version`` disagreement and checksum changes are all refused.
* The very first migration transaction also runs a ``bootstrap`` callback (database
  identity, application id, initial halt row), so a database is either fully created
  or not created at all.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Final

from swing_trading_bot.ledger.codec import ts_to_db
from swing_trading_bot.ledger.errors import (
    IncompleteDatabaseError,
    MigrationChecksumError,
    MigrationError,
    UnsupportedSchemaVersionError,
)

_FILENAME_RE: Final = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_SCHEMA_MIGRATIONS_DDL: Final = (
    """CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY CHECK (version > 0),
  name       TEXT NOT NULL,
  checksum   TEXT NOT NULL CHECK (length(checksum) = 64),
  applied_at TEXT NOT NULL
) STRICT""",
    """CREATE TRIGGER schema_migrations_no_update BEFORE UPDATE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END""",
    """CREATE TRIGGER schema_migrations_no_delete BEFORE DELETE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema_migrations rows cannot be deleted'); END""",
)

Bootstrap = Callable[[sqlite3.Connection], None]
StatementHook = Callable[[int, int], None]
"""Test hook called as ``hook(version, statement_index)`` before each statement."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements(self) -> list[str]:
        statements: list[str] = []
        buffer = ""
        for line in self.sql.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statements.append(buffer.strip())
                buffer = ""
        leftover = "\n".join(
            ln for ln in buffer.splitlines() if ln.strip() and not ln.strip().startswith("--")
        )
        if leftover:
            raise MigrationError(f"migration {self.version}: trailing incomplete statement")
        return statements


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


def packaged_migrations() -> tuple[Migration, ...]:
    package = resources.files("swing_trading_bot.ledger.migrations")
    found: list[Migration] = []
    for entry in package.iterdir():
        match = _FILENAME_RE.match(entry.name)
        if match is None:
            continue
        found.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    found.sort(key=lambda m: m.version)
    validate_sequence(found)
    return tuple(found)


def validate_sequence(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migrations must be numbered contiguously from 1, got {versions}")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [str(r[0]) for r in rows]


def applied_migrations(conn: sqlite3.Connection) -> list[AppliedMigration]:
    if not _table_exists(conn, "schema_migrations"):
        return []
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [AppliedMigration(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def verify_applied(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Return the current schema version after verifying history. Raises on any doubt."""
    applied = applied_migrations(conn)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if not applied:
        if user_version != 0 or _user_tables(conn):
            raise IncompleteDatabaseError(
                "database has tables or a schema version but no migration history"
            )
        return 0
    known = {m.version: m for m in migrations}
    versions = [a.version for a in applied]
    if versions != list(range(1, len(versions) + 1)):
        raise UnsupportedSchemaVersionError(f"migration history has gaps: {versions}")
    newest = versions[-1]
    if newest > len(migrations):
        raise UnsupportedSchemaVersionError(
            f"database schema version {newest} is newer than this application supports "
            f"({len(migrations)})"
        )
    if user_version != newest:
        raise UnsupportedSchemaVersionError(
            f"PRAGMA user_version {user_version} disagrees with migration history {newest}"
        )
    for record in applied:
        expected = known[record.version]
        if record.checksum != expected.checksum or record.name != expected.name:
            raise MigrationChecksumError(
                f"migration {record.version} ({record.name}) differs from the packaged version"
            )
    return newest


def _rollback_if_open(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def apply_pending(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    now: datetime,
    bootstrap: Bootstrap | None = None,
    before_statement: StatementHook | None = None,
) -> list[int]:
    """Apply every pending migration, each atomically. Returns applied versions."""
    if conn.in_transaction:
        raise MigrationError("migrations must not run inside an open transaction")
    validate_sequence(migrations)
    current = verify_applied(conn, migrations)
    if current == 0 and bootstrap is None:
        raise IncompleteDatabaseError("refusing to create a schema without bootstrap data")
    applied: list[int] = []
    for migration in migrations[current:]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if migration.version == 1:
                for ddl in _SCHEMA_MIGRATIONS_DDL:
                    conn.execute(ddl)
            for index, statement in enumerate(migration.statements()):
                if before_statement is not None:
                    before_statement(migration.version, index)
                conn.execute(statement)
            if migration.version == 1 and bootstrap is not None:
                bootstrap(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, ts_to_db(now)),
            )
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except BaseException as exc:
            _rollback_if_open(conn)
            if isinstance(exc, Exception) and not isinstance(exc, MigrationError):
                detail = str(exc.args[0]) if exc.args else type(exc).__name__
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) failed and was rolled "
                    f"back: {detail}"
                ) from exc
            raise
        applied.append(migration.version)
    return applied
