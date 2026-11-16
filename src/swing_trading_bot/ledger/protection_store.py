"""Persistence for protective-stop state (migration 0009; ARCHITECTURE §12.41).

* ``plan_stop_levels`` — append-only, ratcheting stop levels of a plan with their
  evidence (policy P8). :meth:`PlanStopLevelsRepository.append` refuses, before SQL, a
  level below the previous one (exact Decimals); the table trigger is a second guard.
* ``protection_gaps`` — protection gaps of a plan's position (policy P7): a gap opens as
  *potential* (a cancel was requested; the old stop may still work), becomes *confirmed*
  (``confirmed_at``) once no acknowledged stop protects the shares, and closes once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from swing_trading_bot.ledger.codec import (
    dec_from_db,
    dec_to_db,
    int_from_db,
    opt_dec_from_db,
    opt_dec_to_db,
    opt_str_from_db,
    opt_ts_from_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.repositories import canonical_json


class StopLevelSource(StrEnum):
    PLAN_INTENT = "PLAN_INTENT"
    BREAKEVEN = "BREAKEVEN"
    TRAILING = "TRAILING"


@dataclass(frozen=True, slots=True)
class StopLevel:
    trade_plan_id: str
    seq: int
    stop_price: Decimal
    high_water: Decimal | None
    source: StopLevelSource
    evidence: dict[str, Any]
    recorded_at: datetime


class PlanStopLevelsRepository:
    _COLS = "trade_plan_id, seq, stop_price, high_water, source, evidence_json, recorded_at"

    def latest(self, s: Session, trade_plan_id: str) -> StopLevel | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM plan_stop_levels WHERE trade_plan_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (trade_plan_id,),
        )
        return None if row is None else self._from_row(row)

    def list_for_plan(self, s: Session, trade_plan_id: str) -> list[StopLevel]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM plan_stop_levels WHERE trade_plan_id = ? ORDER BY seq",
            (trade_plan_id,),
        )
        return [self._from_row(r) for r in rows]

    def append(
        self,
        tx: Tx,
        trade_plan_id: str,
        *,
        stop_price: Decimal,
        high_water: Decimal | None,
        source: StopLevelSource,
        evidence: dict[str, Any],
        at: datetime,
    ) -> StopLevel:
        if not evidence:
            raise PersistenceError("a stop level needs its evidence")
        prev = self.latest(tx, trade_plan_id)
        if prev is not None:
            if stop_price < prev.stop_price:
                raise PersistenceError(
                    f"stop level {stop_price} below the plan's previous {prev.stop_price}"
                )
            if prev.high_water is not None and (high_water is None or high_water < prev.high_water):
                raise PersistenceError("high-water mark may only ratchet up")
        level = StopLevel(trade_plan_id, 1 if prev is None else prev.seq + 1, stop_price,
                          high_water, source, evidence, at)  # fmt: skip
        tx.execute(
            f"INSERT INTO plan_stop_levels ({self._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_plan_id, level.seq, dec_to_db(stop_price), opt_dec_to_db(high_water),
             source.value, canonical_json(evidence), ts_to_db(at)),
        )  # fmt: skip
        return level

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> StopLevel:
        return StopLevel(
            trade_plan_id=str_from_db(row[0]),
            seq=int_from_db(row[1]),
            stop_price=dec_from_db(row[2]),
            high_water=opt_dec_from_db(row[3]),
            source=StopLevelSource(str_from_db(row[4])),
            evidence=json.loads(str_from_db(row[5])),
            recorded_at=ts_from_db(row[6]),
        )


class GapReason(StrEnum):
    STOP_CANCEL_REQUESTED = "STOP_CANCEL_REQUESTED"
    STOP_ENDED_UNFILLED = "STOP_ENDED_UNFILLED"


class GapCloseReason(StrEnum):
    STOP_ACKNOWLEDGED = "STOP_ACKNOWLEDGED"
    POSITION_FLAT = "POSITION_FLAT"


@dataclass(frozen=True, slots=True)
class ProtectionGap:
    id: int
    trade_plan_id: str
    opened_at: datetime
    reason: GapReason
    prior_client_order_id: str
    confirmed_at: datetime | None
    """When the gap became a confirmed unprotected interval (None: potential only)."""
    closed_at: datetime | None
    close_reason: GapCloseReason | None
    closed_by_client_order_id: str | None

    @property
    def open(self) -> bool:
        return self.closed_at is None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


class ProtectionGapsRepository:
    _COLS = (
        "id, trade_plan_id, opened_at, reason, prior_client_order_id, confirmed_at, closed_at, "
        "close_reason, closed_by_client_order_id"
    )

    def open_gap(self, s: Session, trade_plan_id: str) -> ProtectionGap | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM protection_gaps WHERE trade_plan_id = ? "
            "AND closed_at IS NULL",
            (trade_plan_id,),
        )
        return None if row is None else self._from_row(row)

    def list_for_plan(self, s: Session, trade_plan_id: str) -> list[ProtectionGap]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM protection_gaps WHERE trade_plan_id = ? ORDER BY id",
            (trade_plan_id,),
        )
        return [self._from_row(r) for r in rows]

    def open(
        self, tx: Tx, trade_plan_id: str, *, reason: GapReason, prior: str, at: datetime
    ) -> ProtectionGap:
        """Open a gap, or return the one already open (a gap is never opened twice).
        ``STOP_ENDED_UNFILLED`` opens confirmed; ``STOP_CANCEL_REQUESTED`` opens potential."""
        existing = self.open_gap(tx, trade_plan_id)
        if existing is not None:
            return existing
        confirmed = ts_to_db(at) if reason is GapReason.STOP_ENDED_UNFILLED else None
        tx.execute(
            "INSERT INTO protection_gaps (trade_plan_id, opened_at, reason, "
            "prior_client_order_id, confirmed_at) VALUES (?, ?, ?, ?, ?)",
            (trade_plan_id, ts_to_db(at), reason.value, prior, confirmed),
        )
        gap = self.open_gap(tx, trade_plan_id)
        if gap is None:  # pragma: no cover - just inserted
            raise PersistenceError("protection gap not recorded")
        return gap

    def confirm(self, tx: Tx, gap_id: int, *, at: datetime) -> None:
        n = tx.execute(
            "UPDATE protection_gaps SET confirmed_at = ? WHERE id = ? AND closed_at IS NULL "
            "AND confirmed_at IS NULL",
            (ts_to_db(at), gap_id),
        )
        if n != 1:
            raise PersistenceError(f"protection gap {gap_id} is not an open, potential gap")

    def close(
        self, tx: Tx, gap_id: int, *, reason: GapCloseReason, by: str | None, at: datetime
    ) -> None:
        n = tx.execute(
            "UPDATE protection_gaps SET closed_at = ?, close_reason = ?, "
            "closed_by_client_order_id = ? WHERE id = ? AND closed_at IS NULL",
            (ts_to_db(at), reason.value, by, gap_id),
        )
        if n != 1:
            raise PersistenceError(f"protection gap {gap_id} is not open")

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ProtectionGap:
        close_reason = opt_str_from_db(row[7])
        return ProtectionGap(
            id=int_from_db(row[0]),
            trade_plan_id=str_from_db(row[1]),
            opened_at=ts_from_db(row[2]),
            reason=GapReason(str_from_db(row[3])),
            prior_client_order_id=str_from_db(row[4]),
            confirmed_at=opt_ts_from_db(row[5]),
            closed_at=opt_ts_from_db(row[6]),
            close_reason=None if close_reason is None else GapCloseReason(close_reason),
            closed_by_client_order_id=opt_str_from_db(row[8]),
        )


__all__ = [
    "GapCloseReason",
    "GapReason",
    "PlanStopLevelsRepository",
    "ProtectionGap",
    "ProtectionGapsRepository",
    "StopLevel",
    "StopLevelSource",
]
