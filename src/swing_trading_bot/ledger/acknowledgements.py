"""Event-specific rebaseline acknowledgements (ARCHITECTURE §12.19, migration 0007).

An unexplained broker activity or external broker order is *acknowledged* only when an
APPROVED rebaseline request lists it by id AND the payload recorded then is identical to
the payload stored now. Nothing is acknowledged by time range.
"""

from __future__ import annotations

from typing import Final

from swing_trading_bot.ledger.db import Session

ACKNOWLEDGED_ACTIVITY_SQL: Final = (
    "EXISTS (SELECT 1 FROM rebaseline_acknowledgements k "
    "JOIN rebaseline_requests q ON q.id = k.request_id AND q.status = 'APPROVED' "
    "WHERE k.kind = 'ACTIVITY' AND k.ref_id = broker_activities.activity_id "
    "AND k.payload = broker_activities.raw_json)"
)
"""SQL predicate (usable inside a query on ``broker_activities``)."""


def acknowledged_order(s: Session, client_order_id: str, payload: str) -> bool:
    row = s.fetch_one(
        "SELECT 1 FROM rebaseline_acknowledgements k JOIN rebaseline_requests q "
        "ON q.id = k.request_id AND q.status = 'APPROVED' "
        "WHERE k.kind = 'ORDER' AND k.ref_id = ? AND k.payload = ?",
        (client_order_id, payload),
    )
    return row is not None


__all__ = ["ACKNOWLEDGED_ACTIVITY_SQL", "acknowledged_order"]
