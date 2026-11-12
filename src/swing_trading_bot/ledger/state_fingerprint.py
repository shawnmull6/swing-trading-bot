"""Fingerprint of the ledger state a risk decision depends on (M1 step 11, step 12).

A :class:`~swing_trading_bot.risk.pretrade.RiskDecision` records this hash and step 12
recomputes it inside the transaction that creates the order and its reservation; any
difference refuses the submission, so a decision is never reused after its evidence
has moved.

Completeness by construction: the hash covers the **full ordered contents of every
ledger table** except the ones listed in :data:`EXCLUDED_TABLES` with a reason. Mutable
columns (order status and fill totals, reservation remaining/status, settlement dates
filled in later, positions, marks, sync state, the halt flag, superseded baselines,
rebaseline request status) are therefore all included, not just row counts. A test
fails if a migration adds a table that is neither hashed nor explicitly excluded.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Final

from swing_trading_bot.ledger.db import Session

EXCLUDED_TABLES: Final[dict[str, str]] = {
    "schema_migrations": "migration bookkeeping; changes only with the schema itself",
    "database_identity": "immutable after creation (trigger-enforced)",
}


def fingerprinted_tables(s: Session) -> list[str]:
    rows = s.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [str(r[0]) for r in rows if str(r[0]) not in EXCLUDED_TABLES]


def ledger_fingerprint(s: Session, tables: Iterable[str] | None = None) -> str:
    """SHA-256 over every row of every fingerprinted table, read in one session.

    Rows are taken in rowid order with each value tagged by its SQLite type, so a
    value change, an insert, or a type change all alter the hash.
    """
    digest = hashlib.sha256()
    chosen = fingerprinted_tables(s) if tables is None else sorted(tables)
    for table in chosen:
        digest.update(f"\x1e{table}\x1f".encode())
        for row in s.fetch_all(f'SELECT * FROM "{table}" ORDER BY rowid'):  # noqa: S608
            for value in row:
                digest.update(f"{type(value).__name__}:{value!r}\x1f".encode())
            digest.update(b"\x1d")
    return digest.hexdigest()
