"""Consistent, verified SQLite backups (ARCHITECTURE §12.1 ``backup``; migration safety).

A backup is taken with SQLite's online backup API from a connection that holds no
transaction, so the copy is a consistent snapshot even in WAL mode. The caller must
hold the ledger's exclusive lock (``app/lock.py``) so nothing writes meanwhile.

A backup is only reported as successful after it has been **verified** from a fresh
connection: ``PRAGMA integrity_check`` is ``ok``, the application id and schema
version equal the source's, and a content digest over the schema and every row of
every table equals the source's digest. A copy that fails verification is left in
place under its requested name for inspection and the operation raises
:class:`BackupVerificationError`; it must never be used.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from swing_trading_bot.ledger.db import APPLICATION_ID, require_sqlite_header
from swing_trading_bot.ledger.errors import PersistenceError


class BackupError(PersistenceError):
    """A backup could not be taken (bad destination, unreadable source)."""


class BackupVerificationError(BackupError):
    """A backup was written but does not verify against its source."""


@dataclass(frozen=True, slots=True)
class BackupReport:
    source: Path
    destination: Path
    schema_version: int
    digest: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "schema_version": self.schema_version,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "verified": True,
        }


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise BackupError(f"unexpected table name {name!r}")
    return f'"{name}"'


def content_digest(conn: sqlite3.Connection) -> str:
    """SHA-256 over the schema (sqlite_master), the application id, the schema version
    and every row of every table in a deterministic order."""
    h = hashlib.sha256()
    h.update(repr(int(conn.execute("PRAGMA application_id").fetchone()[0])).encode())
    h.update(repr(int(conn.execute("PRAGMA user_version").fetchone()[0])).encode())
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    for obj in objects:
        h.update(repr(tuple(obj)).encode())
    for obj_type, name, _sql in objects:
        if obj_type != "table":
            continue
        table = _quote(str(name))
        width = len(conn.execute(f"PRAGMA table_info({table})").fetchall())
        order = ", ".join(str(i) for i in range(1, width + 1))
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")  # noqa: S608
        h.update(table.encode())
        for row in rows:
            h.update(repr(tuple((type(v).__name__, v) for v in row)).encode())
    return h.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def _copy(source: sqlite3.Connection, destination: Path) -> None:
    """Online backup of ``source`` into ``destination`` (an existing empty file)."""
    target = sqlite3.connect(destination, isolation_level=None)
    try:
        source.backup(target)
    finally:
        target.close()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_backup(destination: Path, *, digest: str, schema_version: int) -> None:
    try:
        require_sqlite_header(destination)
    except PersistenceError as exc:
        raise BackupVerificationError(f"backup {destination}: {exc}") from None
    try:
        conn = _open_readonly(destination)
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"backup {destination} cannot be opened: {exc}") from None
    try:
        integrity = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise BackupVerificationError(f"backup integrity check failed: {integrity[:3]}")
        app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        if app_id != APPLICATION_ID:
            raise BackupVerificationError("backup does not carry the ledger application id")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != schema_version:
            raise BackupVerificationError(
                f"backup schema version {version} differs from the source ({schema_version})"
            )
        if content_digest(conn) != digest:
            raise BackupVerificationError("backup content differs from the source")
    except sqlite3.DatabaseError as exc:
        raise BackupVerificationError(f"backup is not a valid database: {exc}") from None
    finally:
        conn.close()


def backup_database(source: Path, destination: Path) -> BackupReport:
    """Write a verified, consistent copy of ``source`` to a NEW file ``destination``."""
    source = source.resolve()
    destination = destination.absolute()
    if not source.is_file():
        raise BackupError(f"source ledger not found: {source}")
    require_sqlite_header(source)
    if destination.exists() or destination.is_symlink():
        raise BackupError(f"backup destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise BackupError(f"backup directory does not exist: {destination.parent}")
    if destination.resolve() == source:
        raise BackupError("backup destination is the ledger itself")
    try:
        conn = _open_readonly(source)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open source ledger: {exc}") from None
    try:
        integrity = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise BackupError(f"source ledger fails integrity_check: {integrity[:3]}")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        digest = content_digest(conn)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        _copy(conn, destination)
    except sqlite3.Error as exc:
        raise BackupError(f"backup failed: {exc}") from None
    finally:
        conn.close()
    verify_backup(destination, digest=digest, schema_version=version)
    _fsync(destination)
    _fsync(destination.parent)
    return BackupReport(
        source=source,
        destination=destination,
        schema_version=version,
        digest=digest,
        size_bytes=destination.stat().st_size,
    )
