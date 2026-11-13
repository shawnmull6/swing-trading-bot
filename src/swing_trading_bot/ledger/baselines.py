"""Broker baselines (ARCHITECTURE §8.1): the persisted anchor for broker-side cash.

The paper account holds far more than the bot's $5,000 virtual allocation; broker cash
is reconciled against ``baseline.cash + Σ net amounts of every broker activity since
the baseline``, never against virtual cash. Baselines are immutable rows (trigger-
enforced). :meth:`BrokerBaselinesRepository.replace_for_rebaseline` is the only way to
change which baseline is active: it marks the old row superseded (set-once) and inserts
the new one linked to an APPROVED rebaseline request, in the caller's transaction (the
audited workflow, :mod:`swing_trading_bot.oms.rebaseline`). Old baselines are kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from swing_trading_bot.ledger.codec import (
    dec_from_db,
    dec_to_db,
    int_from_db,
    opt_str_from_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.repositories import canonical_json
from swing_trading_bot.ledger.sync_keys import BASELINE_REFLECTED_ACTIVITIES


@dataclass(frozen=True, slots=True)
class BrokerBaseline:
    id: int
    portfolio_id: int
    taken_at: datetime
    cash: Decimal
    equity: Decimal
    activity_cursor_id: str | None
    account_config: dict[str, Any]
    reason: str


class BrokerBaselinesRepository:
    _COLS = (
        "id, portfolio_id, taken_at, cash, equity, activity_cursor_id, account_config_json, reason"
    )

    def record_initial(
        self,
        tx: Tx,
        *,
        portfolio_id: int,
        taken_at: datetime,
        cash: Decimal,
        equity: Decimal,
        account_config: dict[str, Any],
        activity_cursor_id: str | None = None,
    ) -> int:
        if self.active(tx, portfolio_id) is not None:
            raise PersistenceError("an active broker baseline already exists; use rebaseline")
        return tx.insert(
            "INSERT INTO broker_baselines (portfolio_id, taken_at, cash, equity, "
            "activity_cursor_id, account_config_json, reason) VALUES (?, ?, ?, ?, ?, ?, 'INIT')",
            (portfolio_id, ts_to_db(taken_at), dec_to_db(cash), dec_to_db(equity),
             activity_cursor_id, canonical_json(account_config)),
        )  # fmt: skip

    def replace_for_rebaseline(
        self,
        tx: Tx,
        *,
        portfolio_id: int,
        request_id: int,
        taken_at: datetime,
        cash: Decimal,
        equity: Decimal,
        account_config: dict[str, Any],
    ) -> int:
        current = self.active(tx, portfolio_id)
        if current is None:
            raise PersistenceError("no active baseline to supersede")
        n = tx.execute(
            "UPDATE broker_baselines SET superseded_at = ? WHERE id = ? AND superseded_at IS NULL",
            (ts_to_db(taken_at), current.id),
        )
        if n != 1:
            raise PersistenceError("baseline was superseded concurrently")
        return tx.insert(
            "INSERT INTO broker_baselines (portfolio_id, taken_at, cash, equity, "
            "activity_cursor_id, account_config_json, reason, rebaseline_request_id) "
            "VALUES (?, ?, ?, ?, NULL, ?, 'REBASELINE', ?)",
            (portfolio_id, ts_to_db(taken_at), dec_to_db(cash), dec_to_db(equity),
             canonical_json(account_config), request_id),
        )  # fmt: skip

    def history(self, s: Session, portfolio_id: int) -> list[tuple[int, str, str | None]]:
        """(id, reason, superseded_at) of every baseline ever recorded, oldest first."""
        rows = s.fetch_all(
            "SELECT id, reason, superseded_at FROM broker_baselines WHERE portfolio_id = ? "
            "ORDER BY id",
            (portfolio_id,),
        )
        return [(int_from_db(r[0]), str_from_db(r[1]), opt_str_from_db(r[2])) for r in rows]

    def inception(self, s: Session, portfolio_id: int) -> datetime | None:
        """When the first (INIT) baseline was taken: the start of reconciled history."""
        row = s.fetch_one(
            "SELECT min(taken_at) FROM broker_baselines WHERE portfolio_id = ?", (portfolio_id,)
        )
        return None if row is None or row[0] is None else ts_from_db(row[0])

    def active(self, s: Session, portfolio_id: int) -> BrokerBaseline | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM broker_baselines "  # noqa: S608
            "WHERE portfolio_id = ? AND superseded_at IS NULL",
            (portfolio_id,),
        )
        if row is None:
            return None
        return BrokerBaseline(
            id=int_from_db(row[0]),
            portfolio_id=int_from_db(row[1]),
            taken_at=ts_from_db(row[2]),
            cash=dec_from_db(row[3]),
            equity=dec_from_db(row[4]),
            activity_cursor_id=opt_str_from_db(row[5]),
            account_config=json.loads(str_from_db(row[6])),
            reason=str_from_db(row[7]),
        )


def reflected_activities(s: Session) -> dict[str, str | None]:
    """Activities already inside the INIT baseline cash (Step 15B.13A), by identity.

    ``init-ledger`` reads a complete (single-page, tail-confirmed) activity window before
    it records the broker cash, but stores none of those activities. They are recorded
    as ``{activity_id: raw_json}`` under :data:`BASELINE_REFLECTED_ACTIVITIES`. A ledger
    initialized before Step 15B.13A has only the INIT baseline's ``activity_cursor_id``
    (the last id of that window): it alone is reflected, without a payload (``None``) —
    any other historical activity then fails closed. Never a timestamp range."""
    raw = s.scalar("SELECT value FROM sync_state WHERE key = ?", (BASELINE_REFLECTED_ACTIVITIES,))
    if raw is not None:
        try:
            doc = json.loads(str(raw))
        except ValueError:
            raise PersistenceError("baseline reflected activities are not valid JSON") from None
        if not isinstance(doc, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in doc.items()
        ):
            raise PersistenceError("baseline reflected activities are malformed")
        return dict(doc)
    row = s.fetch_one(
        "SELECT activity_cursor_id FROM broker_baselines WHERE reason = 'INIT' "
        "AND activity_cursor_id IS NOT NULL ORDER BY id LIMIT 1"
    )
    return {} if row is None else {str_from_db(row[0]): None}


def is_baseline_reflected(
    reflected: dict[str, str | None], activity_id: str, raw_json: str
) -> bool:
    """True only for an activity ``init-ledger`` observed — with the identical payload
    whenever the payload was recorded. A changed payload is not reflected: its financial
    effect is not established, so it is treated as new (and fails closed)."""
    if activity_id not in reflected:
        return False
    expected = reflected[activity_id]
    return expected is None or expected == raw_json


def movements_since(s: Session, baseline: BrokerBaseline) -> Decimal:
    """Σ net cash of every broker activity first ingested AFTER the baseline was taken,
    except activities already reflected in the INIT baseline (Step 15B.13A).

    Activities ingested at or before the baseline observation were already reflected in
    the broker cash it recorded (the observation followed a complete activity read), so
    they are inside ``baseline.cash``. So are the activities ``init-ledger`` observed but
    did not store (:func:`reflected_activities`), even when they are first ingested
    later. Using the ingestion time — not the activity's own timestamp — means a
    late-posted activity with an earlier timestamp is counted once, when it is first
    seen, never silently folded into a time range."""
    reflected = reflected_activities(s)
    rows = s.fetch_all(
        "SELECT activity_id, raw_json, net_amount FROM broker_activities "
        "WHERE ingested_at > ? AND net_amount IS NOT NULL",
        (ts_to_db(baseline.taken_at),),
    )
    return sum(
        (
            dec_from_db(r[2])
            for r in rows
            if not is_baseline_reflected(reflected, str_from_db(r[0]), str_from_db(r[1]))
        ),
        Decimal(0),
    )


def cash_residual(s: Session, baseline: BrokerBaseline, broker_cash: Decimal) -> Decimal:
    """R = broker cash - (baseline cash + movements since the baseline): broker cash that
    moved with NO broker activity to show for it."""
    return broker_cash - (baseline.cash + movements_since(s, baseline))


__all__ = [
    "BrokerBaseline",
    "BrokerBaselinesRepository",
    "cash_residual",
    "is_baseline_reflected",
    "movements_since",
    "reflected_activities",
]
