"""The explicit, backed-up migration workflow (M1 step 14, ARCHITECTURE §12.20).

``Database.open`` never migrates an observe (account-backed) ledger, and never migrates
any ledger when ``auto_migrate=False``. This module is the only path that applies
migrations to an existing ledger outside simulation auto-migration:

1. verify the file is this application's ledger, of the expected mode;
2. verify the recorded migration history against the packaged migrations (gaps, a
   newer schema, checksum or name changes, ``user_version`` disagreement → refused);
3. verify physical integrity (``quick_check`` and foreign keys);
4. take a consistent online backup to a NEW file and verify it (``ledger/backup.py``);
5. apply every pending migration in ONE transaction (all or nothing), so a failure or a
   crash leaves the original ledger at its previous version — the backup is a second,
   independent copy;
6. re-open the ledger normally (identity, mode, integrity) to prove it is usable.

The caller must hold the ledger's exclusive lock for the whole operation
(``app/lock.py``); the lock is what refuses concurrent operations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.backup import BackupReport, backup_database
from swing_trading_bot.ledger.db import (
    APPLICATION_ID,
    DEFAULT_BUSY_TIMEOUT_MS,
    Database,
    _connect,
    _stored_mode,
    _verify_physical_integrity,
)
from swing_trading_bot.ledger.errors import (
    DatabaseNotFoundError,
    ForeignDatabaseError,
    IncompleteDatabaseError,
    ModeMismatchError,
    wrap_sqlite_error,
)
from swing_trading_bot.ledger.migrator import (
    Migration,
    StatementHook,
    apply_pending_atomic,
    pending_migrations,
    verify_applied,
)


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    path: Path
    mode: RuntimeMode
    current_version: int
    pending: tuple[int, ...]

    @property
    def target_version(self) -> int:
        return self.pending[-1] if self.pending else self.current_version


@dataclass(frozen=True, slots=True)
class MigrationReport:
    plan: MigrationPlan
    applied: tuple[int, ...]
    backup: BackupReport | None

    def to_dict(self) -> dict[str, object]:
        return {
            "ledger": str(self.plan.path),
            "mode": self.plan.mode.value,
            "from_version": self.plan.current_version,
            "to_version": self.plan.target_version if self.applied else self.plan.current_version,
            "applied": list(self.applied),
            "backup": None if self.backup is None else self.backup.to_dict(),
        }


def _open_checked(path: Path, mode: RuntimeMode) -> sqlite3.Connection:
    if not path.is_file():
        raise DatabaseNotFoundError(f"database not found: {path}")
    conn = _connect(path, DEFAULT_BUSY_TIMEOUT_MS)
    try:
        app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        if app_id == 0:
            raise IncompleteDatabaseError(f"{path} carries no application id")
        if app_id != APPLICATION_ID:
            raise ForeignDatabaseError(f"{path} was not created by this application")
        stored = _stored_mode(conn)
        if stored is None:
            raise IncompleteDatabaseError(f"{path} has no database identity")
        if stored is not mode:
            raise ModeMismatchError(
                f"database {path} is a '{stored.value}' ledger; refusing to migrate it as "
                f"'{mode.value}'"
            )
    except sqlite3.Error as exc:
        conn.close()
        raise wrap_sqlite_error(exc, "open") from None
    except BaseException:
        conn.close()
        raise
    return conn


def plan_migration(path: Path, mode: RuntimeMode, migrations: Sequence[Migration]) -> MigrationPlan:
    """Read-only: verify the ledger and its history and list pending migrations."""
    conn = _open_checked(path, mode)
    try:
        current = verify_applied(conn, migrations)
        pending = pending_migrations(conn, migrations)
        _verify_physical_integrity(conn)
    finally:
        conn.close()
    return MigrationPlan(path, mode, current, tuple(m.version for m in pending))


def migrate_ledger(
    path: Path,
    *,
    mode: RuntimeMode,
    backup_path: Path,
    migrations: Sequence[Migration],
    now: datetime,
    before_statement: StatementHook | None = None,
) -> MigrationReport:
    """Backed-up, all-or-nothing migration of an existing ledger. Caller holds the lock."""
    plan = plan_migration(path, mode, migrations)
    if not plan.pending:
        return MigrationReport(plan, (), None)
    backup = backup_database(path, backup_path)
    conn = _open_checked(path, mode)
    try:
        applied = apply_pending_atomic(conn, migrations, now=now, before_statement=before_statement)
    finally:
        conn.close()
    Database.open(path, mode, now=now, migrations=migrations, auto_migrate=False).close()
    return MigrationReport(plan, tuple(applied), backup)
