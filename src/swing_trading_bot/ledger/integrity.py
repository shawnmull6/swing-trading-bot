"""Database integrity report (physical + structural), for startup and ``status``.

Physical checks are SQLite's own (``quick_check`` / ``integrity_check``,
``foreign_key_check``). Structural checks confirm the application identity, schema
version and singleton rows. Ledger *invariants* (data consistency) live in
``invariants.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from swing_trading_bot.ledger.db import APPLICATION_ID, Session
from swing_trading_bot.ledger.migrator import Migration, packaged_migrations


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    sqlite_check: tuple[str, ...]
    foreign_key_violations: int
    application_id_ok: bool
    schema_version: int
    expected_schema_version: int
    identity_present: bool
    trading_state_present: bool

    @property
    def ok(self) -> bool:
        return (
            self.sqlite_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.application_id_ok
            and self.schema_version == self.expected_schema_version
            and self.identity_present
            and self.trading_state_present
        )

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.sqlite_check != ("ok",):
            out.extend(f"sqlite: {line}" for line in self.sqlite_check[:10])
        if self.foreign_key_violations:
            out.append(f"{self.foreign_key_violations} foreign key violation(s)")
        if not self.application_id_ok:
            out.append("application id mismatch")
        if self.schema_version != self.expected_schema_version:
            out.append(f"schema version {self.schema_version} != {self.expected_schema_version}")
        if not self.identity_present:
            out.append("database_identity row missing")
        if not self.trading_state_present:
            out.append("trading_state row missing")
        return out


def integrity_report(
    s: Session, *, full: bool = False, migrations: tuple[Migration, ...] | None = None
) -> IntegrityReport:
    """Run integrity checks inside the given session (a read snapshot is recommended)."""
    known = migrations if migrations is not None else packaged_migrations()
    check = s.fetch_all("PRAGMA integrity_check" if full else "PRAGMA quick_check")
    fk = s.fetch_all("PRAGMA foreign_key_check")
    app_id = s.scalar("PRAGMA application_id")
    version = s.scalar("PRAGMA user_version")
    identity = s.scalar("SELECT count(*) FROM database_identity WHERE id = 1")
    state = s.scalar("SELECT count(*) FROM trading_state WHERE id = 1")
    return IntegrityReport(
        sqlite_check=tuple(str(r[0]) for r in check),
        foreign_key_violations=len(fk),
        application_id_ok=app_id == APPLICATION_ID,
        schema_version=int(str(version)),
        expected_schema_version=len(known),
        identity_present=identity == 1,
        trading_state_present=state == 1,
    )
