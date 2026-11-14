"""Fresh broker-vs-ledger quantity verification before irreversible loss events
(Step 16.9, ARCHITECTURE §12.37).

Daily-lockout, repeat and weekly-drawdown events are append-only and cannot be undone.
They value the ledger's quantities at fresh marks, so a broker-side quantity change the
ledger has not booked (an overnight split, a fill not yet ingested) would turn into a
permanent false breach. Before any such event is written, the cycle reads the broker's
positions (a GET, outside any transaction) and compares exact quantities with the ledger
positions. Only a ``VERIFIED`` result that is fresh and was taken against the very same
ledger quantities used for valuation permits new loss events.

* ``VERIFIED``     — every symbol held by either side has equal, whole quantities.
* ``INCONCLUSIVE`` — differences only on symbols with booked-fill lag (the broker reports
  more filled quantity on a ledger order than the ledger has booked): the next sync may
  explain them. Entries blocked, loss events deferred, no halt.
* ``MISMATCH``     — a difference without lag evidence, or a negative/fractional broker
  quantity. Entries blocked, loss events deferred, and the runtime raises the existing
  ``RECON_POSITION`` persistent halt (recovery: reconcile + audited resume).
* ``UNAVAILABLE``  — the positions read failed. Entries blocked, loss events deferred; the
  read error follows the existing transport, outage and halt policies.

The latest result is stored in ``sync_state`` (no schema change) so read-only gate
evaluation sees it across processes and restarts. A later ``VERIFIED`` result replaces
it — that is new evidence, not an automatic clearing: a ``MISMATCH`` also left a
persistent halt that only the audited resume path can clear.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.models import BrokerPosition
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.codec import ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    PositionsRepository,
    SyncStateRepository,
)

VERIFICATION_KEY: Final = "positions.verification"
DEFAULT_MAX_AGE: Final = timedelta(minutes=2)


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class PositionVerification:
    status: VerificationStatus
    verified_at: datetime
    ledger_qty: Mapping[str, Decimal]
    """Non-zero ledger quantities the comparison used (the valuation must use these)."""
    broker_qty: Mapping[str, Decimal] | None
    differences: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    def permits_loss_events(
        self, *, now: datetime, ledger_qty: Mapping[str, Decimal], max_age: timedelta
    ) -> str | None:
        """None when new loss events may be written at ``now`` for a valuation that used
        ``ledger_qty``; otherwise the reason they may not."""
        if not self.verified:
            return f"position verification {self.status.value}: {self.detail}".rstrip(": ")
        if abs(now - self.verified_at) > max_age:
            return f"position verification is stale ({self.verified_at.isoformat()})"
        if dict(_nonzero(ledger_qty)) != dict(self.ledger_qty):
            return "ledger quantities changed after the position verification"
        return None

    def to_json(self) -> str:
        return json.dumps(
            {
                "status": self.status.value,
                "verified_at": ts_to_db(self.verified_at),
                "ledger_qty": {k: str(v) for k, v in sorted(self.ledger_qty.items())},
                "broker_qty": None
                if self.broker_qty is None
                else {k: str(v) for k, v in sorted(self.broker_qty.items())},
                "differences": list(self.differences),
                "detail": self.detail,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> PositionVerification:
        doc = json.loads(text)
        broker = doc["broker_qty"]
        return cls(
            status=VerificationStatus(doc["status"]),
            verified_at=ts_from_db(doc["verified_at"]),
            ledger_qty={k: Decimal(v) for k, v in doc["ledger_qty"].items()},
            broker_qty=None if broker is None else {k: Decimal(v) for k, v in broker.items()},
            differences=tuple(doc["differences"]),
            detail=str(doc["detail"]),
        )


def _nonzero(qty: Mapping[str, Decimal]) -> Iterable[tuple[str, Decimal]]:
    return ((k, v) for k, v in sorted(qty.items()) if v != ZERO)


def ledger_quantities(s: Session, portfolio_id: int) -> dict[str, Decimal]:
    return {
        p.symbol: p.qty
        for p in PositionsRepository().list_for_portfolio(s, portfolio_id)
        if p.qty != ZERO
    }


def fill_lag_symbols(s: Session) -> set[str]:
    """Symbols of ledger orders whose broker-reported filled quantity exceeds the booked
    fills (the reconciler's FILL_ACTIVITY_LAG condition, from ledger state only)."""
    return {
        o.symbol
        for o in OrdersRepository().list_all(s)
        if o.broker_filled_qty is not None and o.broker_filled_qty > o.filled_qty
    }


def compare_positions(
    ledger_qty: Mapping[str, Decimal],
    broker: Sequence[BrokerPosition] | None,
    *,
    lag_symbols: Iterable[str],
    at: datetime,
    unavailable_detail: str = "",
) -> PositionVerification:
    ledger = dict(_nonzero(ledger_qty))
    if broker is None:
        detail = unavailable_detail or "broker positions unavailable"
        return PositionVerification(VerificationStatus.UNAVAILABLE, at, ledger, None, detail=detail)
    broker_qty: dict[str, Decimal] = {}
    unsupported: list[str] = []
    for p in broker:
        if p.symbol in broker_qty:
            unsupported.append(f"{p.symbol}: duplicate broker position")
            continue
        broker_qty[p.symbol] = p.qty
        if p.qty < ZERO or p.qty != p.qty.to_integral_value():
            unsupported.append(f"{p.symbol}: broker qty {p.qty} is not a whole long position")
    lagging = set(lag_symbols)
    diffs: list[str] = []
    lag_only = True
    for symbol in sorted(set(ledger) | {k for k, v in broker_qty.items() if v != ZERO}):
        lq, bq = ledger.get(symbol, ZERO), broker_qty.get(symbol, ZERO)
        if lq != bq:
            diffs.append(f"{symbol}: broker {bq} != ledger {lq}")
            lag_only = lag_only and symbol in lagging
    if unsupported:
        found = tuple(unsupported + diffs)
        return PositionVerification(
            VerificationStatus.MISMATCH, at, ledger, broker_qty, found, "; ".join(found)
        )
    if not diffs:
        return PositionVerification(VerificationStatus.VERIFIED, at, ledger, broker_qty)
    status = VerificationStatus.INCONCLUSIVE if lag_only else VerificationStatus.MISMATCH
    return PositionVerification(status, at, ledger, broker_qty, tuple(diffs), "; ".join(diffs))


class PositionVerificationStore:
    def save(self, tx: Tx, verification: PositionVerification) -> None:
        SyncStateRepository().set(
            tx, VERIFICATION_KEY, verification.to_json(), verification.verified_at
        )

    def latest(self, s: Session) -> PositionVerification | None:
        raw = SyncStateRepository().get(s, VERIFICATION_KEY)
        return None if raw is None else PositionVerification.from_json(raw)


__all__ = [
    "DEFAULT_MAX_AGE",
    "VERIFICATION_KEY",
    "PositionVerification",
    "PositionVerificationStore",
    "VerificationStatus",
    "compare_positions",
    "fill_lag_symbols",
    "ledger_quantities",
]
