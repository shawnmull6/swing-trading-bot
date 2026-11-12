"""Durable activity-coverage marker (ARCHITECTURE §12.11).

The marker ``activities.complete_through`` moves forward only for a *complete* window
(tail confirmed and closed past the posting lag), never backwards, and is meant to be
written in the same transaction that ingests that window's activities. An incomplete or
interrupted window leaves it unchanged, so coverage can never be overstated.
"""

from __future__ import annotations

from datetime import datetime

from swing_trading_bot.ledger.codec import ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.repositories import SyncStateRepository
from swing_trading_bot.ledger.sync_keys import ACTIVITIES_COMPLETE_THROUGH

_sync = SyncStateRepository()


def complete_through(s: Session) -> datetime | None:
    value = _sync.get(s, ACTIVITIES_COMPLETE_THROUGH)
    return None if value is None else ts_from_db(value)


def advance_coverage(
    tx: Tx, *, after: datetime | None, until: datetime, complete: bool, at: datetime
) -> bool:
    """Advance the marker to ``until`` for a complete window that starts at or before the
    current marker (no gap) and ends beyond it; returns whether it moved. Callers pass
    ``ActivityWindow.after/until/complete`` (the ledger does not import the broker)."""
    if not complete:
        return False
    current = complete_through(tx)
    if current is not None and after is not None and after > current:
        return False  # a gap between the covered period and this window
    if current is not None and until <= current:
        return False
    _sync.set(tx, ACTIVITIES_COMPLETE_THROUGH, ts_to_db(until), at)
    return True
