"""Repositories for official session closes, equity baselines and loss-limit events
(migration 0003). All three tables are append-only; writes are idempotent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from swing_trading_bot.ledger.codec import (
    date_from_db,
    date_to_db,
    dec_from_db,
    dec_to_db,
    int_from_db,
    opt_date_from_db,
    opt_date_to_db,
    opt_str_from_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.repositories import IngestResult, canonical_json


class CloseSource(StrEnum):
    SESSION_CLOSE_BAR = "SESSION_CLOSE_BAR"
    SIM = "SIM"


@dataclass(frozen=True, slots=True)
class SessionClose:
    symbol: str
    session_date: date
    close_price: Decimal
    source: CloseSource
    as_of: datetime
    received_at: datetime
    feed: str | None = None


class SessionClosesRepository:
    _COLS = "symbol, session_date, close_price, source, as_of, received_at, feed"

    def record(self, tx: Tx, close: SessionClose) -> IngestResult:
        """Insert once; an identical re-record is DUPLICATE, a different price CONFLICT."""
        if close.close_price <= 0:
            raise ValueError("close price must be positive")
        existing = self.get(tx, close.symbol, close.session_date)
        if existing is not None:
            same = (existing.close_price, existing.source) == (close.close_price, close.source)
            return IngestResult.DUPLICATE if same else IngestResult.CONFLICT
        tx.execute(
            f"INSERT INTO session_closes ({self._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                close.symbol,
                date_to_db(close.session_date),
                dec_to_db(close.close_price),
                close.source.value,
                ts_to_db(close.as_of),
                ts_to_db(close.received_at),
                close.feed,
            ),
        )
        return IngestResult.INSERTED

    def get(self, s: Session, symbol: str, session_date: date) -> SessionClose | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM session_closes WHERE symbol = ? AND session_date = ?",
            (symbol, date_to_db(session_date)),
        )
        if row is None:
            return None
        return SessionClose(
            symbol=str_from_db(row[0]),
            session_date=date_from_db(row[1]),
            close_price=dec_from_db(row[2]),
            source=CloseSource(str_from_db(row[3])),
            as_of=ts_from_db(row[4]),
            received_at=ts_from_db(row[5]),
            feed=opt_str_from_db(row[6]),
        )


class SnapshotKind(StrEnum):
    SESSION_BASELINE = "SESSION_BASELINE"
    WEEK_BASELINE = "WEEK_BASELINE"


class BaselineBasis(StrEnum):
    SESSION_CLOSE = "SESSION_CLOSE"
    INCEPTION = "INCEPTION"


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    portfolio_id: int
    kind: SnapshotKind
    applies_to: date
    reference_session: date | None
    reference_at: datetime
    basis: BaselineBasis
    equity: Decimal
    cash: Decimal
    market_value: Decimal
    marks: list[dict[str, Any]]
    taken_at: datetime
    id: int | None = None


class EquitySnapshotsRepository:
    _COLS = (
        "id, portfolio_id, kind, applies_to, reference_session, reference_at, basis, equity, "
        "cash, market_value, marks_json, taken_at"
    )

    def insert_once(self, tx: Tx, snap: EquitySnapshot) -> EquitySnapshot:
        """Insert unless a snapshot already exists for (portfolio, kind, applies_to); in
        that case return the stored one unchanged (baselines are never recomputed)."""
        existing = self.get(tx, snap.portfolio_id, snap.kind, snap.applies_to)
        if existing is not None:
            return existing
        new_id = tx.insert(
            "INSERT INTO equity_snapshots (portfolio_id, kind, applies_to, reference_session, "
            "reference_at, basis, equity, cash, market_value, marks_json, taken_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap.portfolio_id,
                snap.kind.value,
                date_to_db(snap.applies_to),
                opt_date_to_db(snap.reference_session),
                ts_to_db(snap.reference_at),
                snap.basis.value,
                dec_to_db(snap.equity),
                dec_to_db(snap.cash),
                dec_to_db(snap.market_value),
                canonical_json(snap.marks),
                ts_to_db(snap.taken_at),
            ),
        )
        stored = self.get(tx, snap.portfolio_id, snap.kind, snap.applies_to)
        if stored is None or stored.id != new_id:  # pragma: no cover - just inserted
            raise RuntimeError("snapshot insert not visible")
        return stored

    def get(
        self, s: Session, portfolio_id: int, kind: SnapshotKind, applies_to: date
    ) -> EquitySnapshot | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM equity_snapshots "
            "WHERE portfolio_id = ? AND kind = ? AND applies_to = ?",
            (portfolio_id, kind.value, date_to_db(applies_to)),
        )
        return None if row is None else self._from_row(row)

    def applies_to_after(
        self, s: Session, portfolio_id: int, kind: SnapshotKind, after: date
    ) -> list[date]:
        rows = s.fetch_all(
            "SELECT applies_to FROM equity_snapshots WHERE portfolio_id = ? AND kind = ? "
            "AND applies_to > ? ORDER BY applies_to",
            (portfolio_id, kind.value, date_to_db(after)),
        )
        return [date_from_db(r[0]) for r in rows]

    def exists_after(self, s: Session, portfolio_id: int, kind: SnapshotKind, after: date) -> bool:
        return (
            s.fetch_one(
                "SELECT 1 FROM equity_snapshots WHERE portfolio_id = ? AND kind = ? "
                "AND applies_to > ? LIMIT 1",
                (portfolio_id, kind.value, date_to_db(after)),
            )
            is not None
        )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> EquitySnapshot:
        return EquitySnapshot(
            id=int_from_db(row[0]),
            portfolio_id=int_from_db(row[1]),
            kind=SnapshotKind(str_from_db(row[2])),
            applies_to=date_from_db(row[3]),
            reference_session=opt_date_from_db(row[4]),
            reference_at=ts_from_db(row[5]),
            basis=BaselineBasis(str_from_db(row[6])),
            equity=dec_from_db(row[7]),
            cash=dec_from_db(row[8]),
            market_value=dec_from_db(row[9]),
            marks=json.loads(str_from_db(row[10])),
            taken_at=ts_from_db(row[11]),
        )


class LossEventKind(StrEnum):
    DAILY_LOCKOUT = "DAILY_LOCKOUT"
    DAILY_REPEAT_HALT = "DAILY_REPEAT_HALT"
    WEEKLY_HALT = "WEEKLY_HALT"


@dataclass(frozen=True, slots=True)
class LossEvent:
    portfolio_id: int
    ts: datetime
    kind: LossEventKind
    applies_to: date
    baseline_snapshot_id: int
    baseline_equity: Decimal
    equity: Decimal
    loss: Decimal
    loss_pct: Decimal
    threshold_pct: Decimal
    detail: dict[str, Any]
    id: int | None = None


class LossLimitEventsRepository:
    _COLS = (
        "id, portfolio_id, ts, kind, applies_to, baseline_snapshot_id, baseline_equity, equity, "
        "loss, loss_pct, threshold_pct, detail_json"
    )

    def insert_once(self, tx: Tx, event: LossEvent) -> tuple[bool, LossEvent]:
        """Returns (inserted, stored). Repeated evaluation never duplicates an event."""
        existing = self.get(tx, event.portfolio_id, event.kind, event.applies_to)
        if existing is not None:
            return False, existing
        tx.insert(
            "INSERT INTO loss_limit_events (portfolio_id, ts, kind, applies_to, "
            "baseline_snapshot_id, baseline_equity, equity, loss, loss_pct, threshold_pct, "
            "detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.portfolio_id,
                ts_to_db(event.ts),
                event.kind.value,
                date_to_db(event.applies_to),
                event.baseline_snapshot_id,
                dec_to_db(event.baseline_equity),
                dec_to_db(event.equity),
                dec_to_db(event.loss),
                dec_to_db(event.loss_pct),
                dec_to_db(event.threshold_pct),
                canonical_json(event.detail),
            ),
        )
        stored = self.get(tx, event.portfolio_id, event.kind, event.applies_to)
        if stored is None:  # pragma: no cover - just inserted
            raise RuntimeError("event insert not visible")
        return True, stored

    def get(
        self, s: Session, portfolio_id: int, kind: LossEventKind, applies_to: date
    ) -> LossEvent | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM loss_limit_events "
            "WHERE portfolio_id = ? AND kind = ? AND applies_to = ?",
            (portfolio_id, kind.value, date_to_db(applies_to)),
        )
        return None if row is None else self._from_row(row)

    def list(self, s: Session, portfolio_id: int, kind: LossEventKind) -> list[LossEvent]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM loss_limit_events WHERE portfolio_id = ? AND kind = ? "
            "ORDER BY applies_to, id",
            (portfolio_id, kind.value),
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> LossEvent:
        return LossEvent(
            id=int_from_db(row[0]),
            portfolio_id=int_from_db(row[1]),
            ts=ts_from_db(row[2]),
            kind=LossEventKind(str_from_db(row[3])),
            applies_to=date_from_db(row[4]),
            baseline_snapshot_id=int_from_db(row[5]),
            baseline_equity=dec_from_db(row[6]),
            equity=dec_from_db(row[7]),
            loss=dec_from_db(row[8]),
            loss_pct=dec_from_db(row[9]),
            threshold_pct=dec_from_db(row[10]),
            detail=json.loads(str_from_db(row[11])),
        )
