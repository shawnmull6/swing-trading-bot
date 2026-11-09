"""Typed persistence repositories (ARCHITECTURE §12).

Repositories contain SQL and value conversion only — no accounting, settlement or
risk logic. Write methods require a :class:`Tx`; read methods accept any
:class:`Session`. Every safety-critical value is passed explicitly (the schema has no
defaults for them).

Broker truth is always recordable: no repository rejects an event because it would
break an accounting invariant. Duplicate broker events are reported as
``DUPLICATE`` (identical content, a no-op) or ``CONFLICT`` (same id, different
content — the caller halts).

Repositories for tables used only by later steps (baselines, rebaseline requests,
adjustments, calendar, equity snapshots, loss-limit events, resolution attempts) are
added in those steps; their tables already exist in migration 0001.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from swing_trading_bot.domain.enums import (
    ActivityAttribution,
    HaltReason,
    IntentPurpose,
    MarkSource,
    OrderRole,
    OrderStatus,
    OrderType,
    ReconciliationResult,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.models import PriceMark
from swing_trading_bot.domain.state_machine import Transition, transition
from swing_trading_bot.ledger.codec import (
    date_to_db,
    dec_from_db,
    dec_to_db,
    int_from_db,
    opt_date_from_db,
    opt_date_to_db,
    opt_dec_from_db,
    opt_dec_to_db,
    opt_int_from_db,
    opt_str_from_db,
    opt_ts_from_db,
    opt_ts_to_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)
from swing_trading_bot.ledger.db import Session, Tx
from swing_trading_bot.ledger.errors import (
    ConcurrencyConflictError,
    PersistenceError,
    RecordNotFoundError,
)


class IngestResult(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


def canonical_json(value: object) -> str:
    """Deterministic JSON (sorted keys, no whitespace); Decimals become strings."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return dec_to_db(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


# --------------------------------------------------------------------- portfolios


@dataclass(frozen=True, slots=True)
class PortfolioRecord:
    name: str
    mode: RuntimeMode
    initial_capital: Decimal
    max_capital_allocation: Decimal
    account_profile: str
    created_at: datetime
    id: int | None = None


class PortfoliosRepository:
    _COLS = "id, name, mode, initial_capital, max_capital_allocation, account_profile, created_at"

    def insert(self, tx: Tx, rec: PortfolioRecord) -> int:
        return tx.insert(
            "INSERT INTO portfolios (name, mode, initial_capital, max_capital_allocation, "
            "account_profile, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                rec.name,
                rec.mode.value,
                dec_to_db(rec.initial_capital),
                dec_to_db(rec.max_capital_allocation),
                rec.account_profile,
                ts_to_db(rec.created_at),
            ),
        )

    def get_by_name(self, s: Session, name: str) -> PortfolioRecord | None:
        row = s.fetch_one(f"SELECT {self._COLS} FROM portfolios WHERE name = ?", (name,))
        return None if row is None else self._from_row(row)

    def get(self, s: Session, portfolio_id: int) -> PortfolioRecord:
        row = s.fetch_one(f"SELECT {self._COLS} FROM portfolios WHERE id = ?", (portfolio_id,))
        if row is None:
            raise RecordNotFoundError(f"portfolio {portfolio_id} not found")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> PortfolioRecord:
        return PortfolioRecord(
            id=int_from_db(row[0]),
            name=str_from_db(row[1]),
            mode=RuntimeMode(str_from_db(row[2])),
            initial_capital=dec_from_db(row[3]),
            max_capital_allocation=dec_from_db(row[4]),
            account_profile=str_from_db(row[5]),
            created_at=ts_from_db(row[6]),
        )


# ------------------------------------------------------------------------- orders


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    portfolio_id: int
    intent_key: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    purpose: IntentPurpose
    qty: Decimal
    limit_price: Decimal | None
    planned_stop: Decimal | None
    status: OrderStatus
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    created_at: datetime
    updated_at: datetime
    broker_order_id: str | None = None
    last_broker_status: str | None = None
    reject_reason: str | None = None
    trade_plan_id: str | None = None
    order_role: OrderRole | None = None
    send_attempted_at: datetime | None = None
    last_synced_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderProgress:
    """Optional fields updated together with a status transition."""

    filled_qty: Decimal | None = None
    avg_fill_price: Decimal | None = None
    last_broker_status: str | None = None
    reject_reason: str | None = None
    last_synced_at: datetime | None = None


class OrdersRepository:
    _COLS = (
        "client_order_id, broker_order_id, portfolio_id, intent_key, symbol, side, order_type, "
        "time_in_force, purpose, qty, limit_price, planned_stop, status, filled_qty, "
        "avg_fill_price, last_broker_status, reject_reason, trade_plan_id, order_role, "
        "created_at, send_attempted_at, updated_at, last_synced_at"
    )

    def insert(self, tx: Tx, rec: OrderRecord) -> None:
        if rec.trade_plan_id is not None or rec.order_role is not None:
            raise PersistenceError("trade_plan_id/order_role are reserved for RTH-01 (NULL in M1)")
        tx.execute(
            f"INSERT INTO orders ({self._COLS}) VALUES ({', '.join('?' * 23)})",
            (
                rec.client_order_id,
                rec.broker_order_id,
                rec.portfolio_id,
                rec.intent_key,
                rec.symbol,
                rec.side.value,
                rec.order_type.value,
                rec.time_in_force.value,
                rec.purpose.value,
                dec_to_db(rec.qty),
                opt_dec_to_db(rec.limit_price),
                opt_dec_to_db(rec.planned_stop),
                rec.status.value,
                dec_to_db(rec.filled_qty),
                opt_dec_to_db(rec.avg_fill_price),
                rec.last_broker_status,
                rec.reject_reason,
                None,
                None,
                ts_to_db(rec.created_at),
                opt_ts_to_db(rec.send_attempted_at),
                ts_to_db(rec.updated_at),
                opt_ts_to_db(rec.last_synced_at),
            ),
        )

    def get(self, s: Session, client_order_id: str) -> OrderRecord | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM orders WHERE client_order_id = ?", (client_order_id,)
        )
        return None if row is None else self._from_row(row)

    def require(self, s: Session, client_order_id: str) -> OrderRecord:
        rec = self.get(s, client_order_id)
        if rec is None:
            raise RecordNotFoundError(f"order {client_order_id} not found")
        return rec

    def get_by_broker_id(self, s: Session, broker_order_id: str) -> OrderRecord | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM orders WHERE broker_order_id = ?", (broker_order_id,)
        )
        return None if row is None else self._from_row(row)

    def list_working(self, s: Session) -> list[OrderRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM orders WHERE status NOT IN "
            "('FILLED','CANCELED','EXPIRED','REJECTED','CONFIRMED_ABSENT','ORPHAN') "
            "ORDER BY created_at, client_order_id"
        )
        return [self._from_row(r) for r in rows]

    def list_by_status(self, s: Session, status: OrderStatus) -> list[OrderRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM orders WHERE status = ? ORDER BY created_at",
            (status.value,),
        )
        return [self._from_row(r) for r in rows]

    def record_send_attempt(self, tx: Tx, client_order_id: str, at: datetime) -> None:
        """Marker written in its own transaction immediately before a network send."""
        n = tx.execute(
            "UPDATE orders SET send_attempted_at = ?, updated_at = ? "
            "WHERE client_order_id = ? AND send_attempted_at IS NULL",
            (ts_to_db(at), ts_to_db(at), client_order_id),
        )
        if n != 1:
            raise ConcurrencyConflictError(f"send attempt already recorded for {client_order_id}")

    def record_fill_progress(
        self,
        tx: Tx,
        client_order_id: str,
        *,
        filled_qty: Decimal,
        avg_fill_price: Decimal | None,
        at: datetime,
    ) -> None:
        """Update fill totals from booked fills. Status changes stay with the order
        lifecycle (broker-confirmed statuses, step 12); this never touches status."""
        n = tx.execute(
            "UPDATE orders SET filled_qty = ?, avg_fill_price = ?, updated_at = ? "
            "WHERE client_order_id = ?",
            (dec_to_db(filled_qty), opt_dec_to_db(avg_fill_price), ts_to_db(at), client_order_id),
        )
        if n != 1:
            raise RecordNotFoundError(f"order {client_order_id} not found")

    def set_broker_order_id(
        self, tx: Tx, client_order_id: str, broker_order_id: str, at: datetime
    ) -> None:
        n = tx.execute(
            "UPDATE orders SET broker_order_id = ?, updated_at = ? WHERE client_order_id = ?",
            (broker_order_id, ts_to_db(at), client_order_id),
        )
        if n != 1:
            raise RecordNotFoundError(f"order {client_order_id} not found")

    def transition_status(
        self,
        tx: Tx,
        client_order_id: str,
        *,
        expected: OrderStatus,
        target: OrderStatus,
        at: datetime,
        progress: OrderProgress | None = None,
    ) -> Transition:
        """Apply a validated status transition with an optimistic status check."""
        result = transition(expected, target)  # raises IllegalTransitionError
        p = progress if progress is not None else OrderProgress()
        n = tx.execute(
            "UPDATE orders SET status = ?, updated_at = ?, "
            "filled_qty = COALESCE(?, filled_qty), "
            "avg_fill_price = COALESCE(?, avg_fill_price), "
            "last_broker_status = COALESCE(?, last_broker_status), "
            "reject_reason = COALESCE(?, reject_reason), "
            "last_synced_at = COALESCE(?, last_synced_at) "
            "WHERE client_order_id = ? AND status = ?",
            (
                target.value,
                ts_to_db(at),
                opt_dec_to_db(p.filled_qty),
                opt_dec_to_db(p.avg_fill_price),
                p.last_broker_status,
                p.reject_reason,
                opt_ts_to_db(p.last_synced_at),
                client_order_id,
                expected.value,
            ),
        )
        if n != 1:
            raise ConcurrencyConflictError(
                f"order {client_order_id} is not in expected status {expected.value}"
            )
        return result

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> OrderRecord:
        role = opt_str_from_db(row[18])
        return OrderRecord(
            client_order_id=str_from_db(row[0]),
            broker_order_id=opt_str_from_db(row[1]),
            portfolio_id=int_from_db(row[2]),
            intent_key=str_from_db(row[3]),
            symbol=str_from_db(row[4]),
            side=Side(str_from_db(row[5])),
            order_type=OrderType(str_from_db(row[6])),
            time_in_force=TimeInForce(str_from_db(row[7])),
            purpose=IntentPurpose(str_from_db(row[8])),
            qty=dec_from_db(row[9]),
            limit_price=opt_dec_from_db(row[10]),
            planned_stop=opt_dec_from_db(row[11]),
            status=OrderStatus(str_from_db(row[12])),
            filled_qty=dec_from_db(row[13]),
            avg_fill_price=opt_dec_from_db(row[14]),
            last_broker_status=opt_str_from_db(row[15]),
            reject_reason=opt_str_from_db(row[16]),
            trade_plan_id=opt_str_from_db(row[17]),
            order_role=None if role is None else OrderRole(role),
            created_at=ts_from_db(row[19]),
            send_attempted_at=opt_ts_from_db(row[20]),
            updated_at=ts_from_db(row[21]),
            last_synced_at=opt_ts_from_db(row[22]),
        )


@dataclass(frozen=True, slots=True)
class OrderEvent:
    client_order_id: str
    ts: datetime
    source: str
    from_status: OrderStatus | None = None
    to_status: OrderStatus | None = None
    detail: dict[str, Any] | None = None


class OrderEventsRepository:
    def append(self, tx: Tx, event: OrderEvent) -> int:
        return tx.insert(
            "INSERT INTO order_events (client_order_id, ts, from_status, to_status, source, "
            "detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.client_order_id,
                ts_to_db(event.ts),
                None if event.from_status is None else event.from_status.value,
                None if event.to_status is None else event.to_status.value,
                event.source,
                None if event.detail is None else canonical_json(event.detail),
            ),
        )

    def list_for_order(self, s: Session, client_order_id: str) -> list[OrderEvent]:
        rows = s.fetch_all(
            "SELECT client_order_id, ts, from_status, to_status, source, detail_json "
            "FROM order_events WHERE client_order_id = ? ORDER BY id",
            (client_order_id,),
        )
        return [
            OrderEvent(
                client_order_id=str_from_db(r[0]),
                ts=ts_from_db(r[1]),
                from_status=None if r[2] is None else OrderStatus(str_from_db(r[2])),
                to_status=None if r[3] is None else OrderStatus(str_from_db(r[3])),
                source=str_from_db(r[4]),
                detail=None if r[5] is None else json.loads(str_from_db(r[5])),
            )
            for r in rows
        ]


# ------------------------------------------------------------------- reservations


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    client_order_id: str
    original: Decimal
    remaining: Decimal
    status: ReservationStatus
    updated_at: datetime


class ReservationsRepository:
    _COLS = "client_order_id, original, remaining, status, updated_at"

    def create(self, tx: Tx, client_order_id: str, amount: Decimal, at: datetime) -> None:
        tx.execute(
            f"INSERT INTO cash_reservations ({self._COLS}) VALUES (?, ?, ?, 'ACTIVE', ?)",
            (client_order_id, dec_to_db(amount), dec_to_db(amount), ts_to_db(at)),
        )

    def get(self, s: Session, client_order_id: str) -> ReservationRecord | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM cash_reservations WHERE client_order_id = ?",
            (client_order_id,),
        )
        return None if row is None else self._from_row(row)

    def list_active(self, s: Session) -> list[ReservationRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM cash_reservations WHERE status = 'ACTIVE' "
            "ORDER BY client_order_id"
        )
        return [self._from_row(r) for r in rows]

    def set_remaining(self, tx: Tx, client_order_id: str, remaining: Decimal, at: datetime) -> None:
        n = tx.execute(
            "UPDATE cash_reservations SET remaining = ?, updated_at = ? "
            "WHERE client_order_id = ? AND status = 'ACTIVE'",
            (dec_to_db(remaining), ts_to_db(at), client_order_id),
        )
        if n != 1:
            raise ConcurrencyConflictError(f"no ACTIVE reservation for {client_order_id}")

    def close(self, tx: Tx, client_order_id: str, status: ReservationStatus, at: datetime) -> None:
        if status is ReservationStatus.ACTIVE:
            raise ValueError("close() requires RELEASED or CONSUMED")
        n = tx.execute(
            "UPDATE cash_reservations SET status = ?, updated_at = ? "
            "WHERE client_order_id = ? AND status = 'ACTIVE'",
            (status.value, ts_to_db(at), client_order_id),
        )
        if n != 1:
            raise ConcurrencyConflictError(f"no ACTIVE reservation for {client_order_id}")

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReservationRecord:
        return ReservationRecord(
            client_order_id=str_from_db(row[0]),
            original=dec_from_db(row[1]),
            remaining=dec_from_db(row[2]),
            status=ReservationStatus(str_from_db(row[3])),
            updated_at=ts_from_db(row[4]),
        )


# ------------------------------------------------------------- broker activities


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    activity_id: str
    activity_type: str
    ts: datetime
    attribution: ActivityAttribution
    raw_json: str
    ingested_at: datetime
    symbol: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    net_amount: Decimal | None = None
    side: Side | None = None
    broker_order_id: str | None = None
    client_order_id: str | None = None

    def content_key(self) -> tuple[object, ...]:
        """Fields that identify the broker event (excludes ingestion metadata)."""
        return (
            self.activity_id,
            self.activity_type,
            self.ts,
            self.symbol,
            self.qty,
            self.price,
            self.net_amount,
            self.side,
            self.broker_order_id,
            self.client_order_id,
            json.loads(self.raw_json),
        )


class ActivitiesRepository:
    _COLS = (
        "activity_id, activity_type, ts, symbol, qty, price, net_amount, side, "
        "broker_order_id, client_order_id, attribution, raw_json, ingested_at"
    )

    def record(self, tx: Tx, rec: ActivityRecord) -> IngestResult:
        """Insert once. Replays are DUPLICATE; same id with different content is CONFLICT."""
        n = tx.execute(
            f"INSERT INTO broker_activities ({self._COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(activity_id) DO NOTHING",
            (
                rec.activity_id,
                rec.activity_type,
                ts_to_db(rec.ts),
                rec.symbol,
                opt_dec_to_db(rec.qty),
                opt_dec_to_db(rec.price),
                opt_dec_to_db(rec.net_amount),
                None if rec.side is None else rec.side.value,
                rec.broker_order_id,
                rec.client_order_id,
                rec.attribution.value,
                rec.raw_json,
                ts_to_db(rec.ingested_at),
            ),
        )
        if n == 1:
            return IngestResult.INSERTED
        stored = self.get(tx, rec.activity_id)
        if stored is None:  # pragma: no cover - conflict implies existence
            raise PersistenceError("activity conflict without a stored row")
        return (
            IngestResult.DUPLICATE
            if stored.content_key() == rec.content_key()
            else IngestResult.CONFLICT
        )

    def get(self, s: Session, activity_id: str) -> ActivityRecord | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM broker_activities WHERE activity_id = ?", (activity_id,)
        )
        if row is None:
            return None
        side = opt_str_from_db(row[7])
        return ActivityRecord(
            activity_id=str_from_db(row[0]),
            activity_type=str_from_db(row[1]),
            ts=ts_from_db(row[2]),
            symbol=opt_str_from_db(row[3]),
            qty=opt_dec_from_db(row[4]),
            price=opt_dec_from_db(row[5]),
            net_amount=opt_dec_from_db(row[6]),
            side=None if side is None else Side(side),
            broker_order_id=opt_str_from_db(row[8]),
            client_order_id=opt_str_from_db(row[9]),
            attribution=ActivityAttribution(str_from_db(row[10])),
            raw_json=str_from_db(row[11]),
            ingested_at=ts_from_db(row[12]),
        )

    def count(self, s: Session) -> int:
        return int_from_db(s.scalar("SELECT count(*) FROM broker_activities"))


# --------------------------------------------------------------------------- fills


@dataclass(frozen=True, slots=True)
class FillRecord:
    activity_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fees: Decimal
    filled_at: datetime
    recorded_at: datetime
    avg_cost_before: Decimal | None = None
    realized_pnl: Decimal | None = None
    id: int | None = None


class FillsRepository:
    _COLS = (
        "id, activity_id, client_order_id, symbol, side, qty, price, fees, avg_cost_before, "
        "realized_pnl, filled_at, recorded_at"
    )

    def insert(self, tx: Tx, rec: FillRecord) -> tuple[IngestResult, int]:
        """Insert a broker-confirmed fill. Never rejected for accounting reasons."""
        existing = self.get_by_activity(tx, rec.activity_id)
        if existing is not None:
            same = (
                existing.client_order_id,
                existing.symbol,
                existing.side,
                existing.qty,
                existing.price,
                existing.fees,
                existing.filled_at,
            ) == (
                rec.client_order_id,
                rec.symbol,
                rec.side,
                rec.qty,
                rec.price,
                rec.fees,
                rec.filled_at,
            )
            if existing.id is None:  # pragma: no cover - stored rows always have ids
                raise PersistenceError("stored fill without id")
            return (IngestResult.DUPLICATE if same else IngestResult.CONFLICT), existing.id
        new_id = tx.insert(
            "INSERT INTO fills (activity_id, client_order_id, symbol, side, qty, price, fees, "
            "avg_cost_before, realized_pnl, filled_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.activity_id,
                rec.client_order_id,
                rec.symbol,
                rec.side.value,
                dec_to_db(rec.qty),
                dec_to_db(rec.price),
                dec_to_db(rec.fees),
                opt_dec_to_db(rec.avg_cost_before),
                opt_dec_to_db(rec.realized_pnl),
                ts_to_db(rec.filled_at),
                ts_to_db(rec.recorded_at),
            ),
        )
        return IngestResult.INSERTED, new_id

    def get_by_activity(self, s: Session, activity_id: str) -> FillRecord | None:
        row = s.fetch_one(f"SELECT {self._COLS} FROM fills WHERE activity_id = ?", (activity_id,))
        return None if row is None else self._from_row(row)

    def list_for_order(self, s: Session, client_order_id: str) -> list[FillRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM fills WHERE client_order_id = ? ORDER BY filled_at, id",
            (client_order_id,),
        )
        return [self._from_row(r) for r in rows]

    def list_in_application_order(self, s: Session) -> list[FillRecord]:
        """Fills in the order they were booked (rowid) — the order accounting used."""
        rows = s.fetch_all(f"SELECT {self._COLS} FROM fills ORDER BY id")
        return [self._from_row(r) for r in rows]

    def list_all(self, s: Session) -> list[FillRecord]:
        rows = s.fetch_all(f"SELECT {self._COLS} FROM fills ORDER BY filled_at, id")
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> FillRecord:
        return FillRecord(
            id=int_from_db(row[0]),
            activity_id=str_from_db(row[1]),
            client_order_id=str_from_db(row[2]),
            symbol=str_from_db(row[3]),
            side=Side(str_from_db(row[4])),
            qty=dec_from_db(row[5]),
            price=dec_from_db(row[6]),
            fees=dec_from_db(row[7]),
            avg_cost_before=opt_dec_from_db(row[8]),
            realized_pnl=opt_dec_from_db(row[9]),
            filled_at=ts_from_db(row[10]),
            recorded_at=ts_from_db(row[11]),
        )


# -------------------------------------------------------------------- cash entries


class CashEntryKind(StrEnum):
    DEPOSIT = "DEPOSIT"
    BUY = "BUY"
    SELL = "SELL"
    FEE = "FEE"
    DIVIDEND = "DIVIDEND"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass(frozen=True, slots=True)
class CashEntryRecord:
    portfolio_id: int
    ts: datetime
    kind: CashEntryKind
    amount: Decimal
    trade_date: date
    settles_on: date | None
    activity_id: str | None = None
    fill_id: int | None = None
    adjustment_id: int | None = None
    memo: str | None = None
    id: int | None = None


class CashEntriesRepository:
    _COLS = (
        "id, portfolio_id, ts, kind, amount, trade_date, settles_on, activity_id, fill_id, "
        "adjustment_id, memo"
    )

    def append(self, tx: Tx, rec: CashEntryRecord) -> int:
        return tx.insert(
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on, "
            "activity_id, fill_id, adjustment_id, memo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.portfolio_id,
                ts_to_db(rec.ts),
                rec.kind.value,
                dec_to_db(rec.amount),
                date_to_db(rec.trade_date),
                opt_date_to_db(rec.settles_on),
                rec.activity_id,
                rec.fill_id,
                rec.adjustment_id,
                rec.memo,
            ),
        )

    def resolve_settlement(self, tx: Tx, entry_id: int, settles_on: date) -> None:
        n = tx.execute(
            "UPDATE cash_entries SET settles_on = ? WHERE id = ? AND settles_on IS NULL",
            (date_to_db(settles_on), entry_id),
        )
        if n != 1:
            raise ConcurrencyConflictError(f"cash entry {entry_id} has no unresolved settlement")

    def list_unresolved_settlements(self, s: Session, portfolio_id: int) -> list[CashEntryRecord]:
        return [e for e in self.list_for_portfolio(s, portfolio_id) if e.settles_on is None]

    def list_for_portfolio(self, s: Session, portfolio_id: int) -> list[CashEntryRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM cash_entries WHERE portfolio_id = ? ORDER BY id",
            (portfolio_id,),
        )
        return [
            CashEntryRecord(
                id=int_from_db(r[0]),
                portfolio_id=int_from_db(r[1]),
                ts=ts_from_db(r[2]),
                kind=CashEntryKind(str_from_db(r[3])),
                amount=dec_from_db(r[4]),
                trade_date=date.fromisoformat(str_from_db(r[5])),
                settles_on=opt_date_from_db(r[6]),
                activity_id=opt_str_from_db(r[7]),
                fill_id=opt_int_from_db(r[8]),
                adjustment_id=opt_int_from_db(r[9]),
                memo=opt_str_from_db(r[10]),
            )
            for r in rows
        ]


# ----------------------------------------------------------------------- positions


@dataclass(frozen=True, slots=True)
class PositionRecord:
    portfolio_id: int
    symbol: str
    qty: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal
    updated_at: datetime
    opened_at: datetime | None = None
    opened_session: date | None = None


class PositionsRepository:
    _COLS = (
        "portfolio_id, symbol, qty, cost_basis, realized_pnl, opened_at, opened_session, updated_at"
    )

    def upsert(self, tx: Tx, rec: PositionRecord) -> None:
        tx.execute(
            f"INSERT INTO positions ({self._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, symbol) DO UPDATE SET qty = excluded.qty, "
            "cost_basis = excluded.cost_basis, realized_pnl = excluded.realized_pnl, "
            "opened_at = excluded.opened_at, opened_session = excluded.opened_session, "
            "updated_at = excluded.updated_at",
            (
                rec.portfolio_id,
                rec.symbol,
                dec_to_db(rec.qty),
                dec_to_db(rec.cost_basis),
                dec_to_db(rec.realized_pnl),
                opt_ts_to_db(rec.opened_at),
                opt_date_to_db(rec.opened_session),
                ts_to_db(rec.updated_at),
            ),
        )

    def get(self, s: Session, portfolio_id: int, symbol: str) -> PositionRecord | None:
        row = s.fetch_one(
            f"SELECT {self._COLS} FROM positions WHERE portfolio_id = ? AND symbol = ?",
            (portfolio_id, symbol),
        )
        return None if row is None else self._from_row(row)

    def list_for_portfolio(self, s: Session, portfolio_id: int) -> list[PositionRecord]:
        rows = s.fetch_all(
            f"SELECT {self._COLS} FROM positions WHERE portfolio_id = ? ORDER BY symbol",
            (portfolio_id,),
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> PositionRecord:
        return PositionRecord(
            portfolio_id=int_from_db(row[0]),
            symbol=str_from_db(row[1]),
            qty=dec_from_db(row[2]),
            cost_basis=dec_from_db(row[3]),
            realized_pnl=dec_from_db(row[4]),
            opened_at=opt_ts_from_db(row[5]),
            opened_session=opt_date_from_db(row[6]),
            updated_at=ts_from_db(row[7]),
        )


# --------------------------------------------------------------------- price marks


class PriceMarksRepository:
    def upsert(self, tx: Tx, mark: PriceMark) -> None:
        tx.execute(
            "INSERT INTO price_marks (symbol, price, as_of, received_at, source, feed) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
            "price = excluded.price, as_of = excluded.as_of, "
            "received_at = excluded.received_at, source = excluded.source, feed = excluded.feed",
            (
                mark.symbol,
                dec_to_db(mark.price),
                ts_to_db(mark.as_of),
                ts_to_db(mark.received_at),
                mark.source.value,
                mark.feed,
            ),
        )

    def get(self, s: Session, symbol: str) -> PriceMark | None:
        row = s.fetch_one(
            "SELECT symbol, price, as_of, received_at, source, feed FROM price_marks "
            "WHERE symbol = ?",
            (symbol,),
        )
        if row is None:
            return None
        return PriceMark(
            symbol=str_from_db(row[0]),
            price=dec_from_db(row[1]),
            as_of=ts_from_db(row[2]),
            received_at=ts_from_db(row[3]),
            source=MarkSource(str_from_db(row[4])),
            feed=opt_str_from_db(row[5]),
        )


# ---------------------------------------------------------------------- sync state


class SyncStateRepository:
    def get(self, s: Session, key: str) -> str | None:
        return opt_str_from_db(s.scalar("SELECT value FROM sync_state WHERE key = ?", (key,)))

    def set(self, tx: Tx, key: str, value: str, at: datetime) -> None:
        tx.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, ts_to_db(at)),
        )


# ------------------------------------------------------------- reconciliation runs


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    id: int
    ts: datetime
    result: ReconciliationResult
    diff: Any


class ReconciliationRunsRepository:
    def append(self, tx: Tx, result: ReconciliationResult, diff: object, at: datetime) -> int:
        return tx.insert(
            "INSERT INTO reconciliation_runs (ts, result, diff_json) VALUES (?, ?, ?)",
            (ts_to_db(at), result.value, None if diff is None else canonical_json(diff)),
        )

    def get(self, s: Session, run_id: int) -> ReconciliationRun | None:
        row = s.fetch_one(
            "SELECT id, ts, result, diff_json FROM reconciliation_runs WHERE id = ?", (run_id,)
        )
        return None if row is None else self._from_row(row)

    def latest(self, s: Session) -> ReconciliationRun | None:
        row = s.fetch_one(
            "SELECT id, ts, result, diff_json FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        )
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReconciliationRun:
        diff_text = opt_str_from_db(row[3])
        return ReconciliationRun(
            id=int_from_db(row[0]),
            ts=ts_from_db(row[1]),
            result=ReconciliationResult(str_from_db(row[2])),
            diff=None if diff_text is None else json.loads(diff_text),
        )


# ------------------------------------------------------------------- trading state


@dataclass(frozen=True, slots=True)
class TradingState:
    halted: bool
    reason_code: str | None
    reason_detail: str | None
    changed_at: datetime
    changed_by: str


@dataclass(frozen=True, slots=True)
class HaltEvent:
    ts: datetime
    action: str
    reason_code: str
    detail: str | None
    actor: str
    reconciliation_run_id: int | None


class ResumeOutcome(StrEnum):
    RESUMED = "RESUMED"
    REFUSED = "REFUSED"


class TradingStateRepository:
    """Persistent halt flag (ARCHITECTURE §7) with an append-only audit trail."""

    def get(self, s: Session) -> TradingState:
        row = s.fetch_one(
            "SELECT halted, reason_code, reason_detail, changed_at, changed_by "
            "FROM trading_state WHERE id = 1"
        )
        if row is None:
            raise RecordNotFoundError("trading_state row is missing")
        return TradingState(
            halted=int_from_db(row[0]) == 1,
            reason_code=opt_str_from_db(row[1]),
            reason_detail=opt_str_from_db(row[2]),
            changed_at=ts_from_db(row[3]),
            changed_by=str_from_db(row[4]),
        )

    def halt(
        self,
        tx: Tx,
        reason: HaltReason,
        *,
        detail: str,
        actor: str,
        at: datetime,
        reconciliation_run_id: int | None = None,
    ) -> None:
        tx.execute(
            "UPDATE trading_state SET halted = 1, reason_code = ?, reason_detail = ?, "
            "changed_at = ?, changed_by = ? WHERE id = 1",
            (reason.value, detail, ts_to_db(at), actor),
        )
        self._event(
            tx,
            action="HALT",
            reason_code=reason.value,
            detail=detail,
            actor=actor,
            at=at,
            run_id=reconciliation_run_id,
        )

    def resume(
        self,
        tx: Tx,
        *,
        detail: str,
        actor: str,
        at: datetime,
        reconciliation_run_id: int,
    ) -> ResumeOutcome:
        """Clear the persistent halt. Requires a referenced OK reconciliation run (D11).

        The application layer decides *whether* to resume; this guard makes it
        impossible to persist a resume without evidence of a clean reconciliation.
        A refusal is returned (not raised) so its audit event commits with the
        transaction; the halt flag is left untouched.
        """
        run = ReconciliationRunsRepository().get(tx, reconciliation_run_id)
        if run is None:
            self._event(
                tx,
                action="RESUME_REFUSED",
                reason_code="RECON_RUN_MISSING",
                detail=f"{detail} [requested reconciliation run {reconciliation_run_id} not found]",
                actor=actor,
                at=at,
                run_id=None,
            )
            return ResumeOutcome.REFUSED
        if run.result is not ReconciliationResult.OK:
            self._event(
                tx,
                action="RESUME_REFUSED",
                reason_code="RECON_NOT_OK",
                detail=detail,
                actor=actor,
                at=at,
                run_id=reconciliation_run_id,
            )
            return ResumeOutcome.REFUSED
        tx.execute(
            "UPDATE trading_state SET halted = 0, reason_code = NULL, reason_detail = ?, "
            "changed_at = ?, changed_by = ? WHERE id = 1",
            (detail, ts_to_db(at), actor),
        )
        self._event(
            tx,
            action="RESUME",
            reason_code="OPERATOR_RESUME",
            detail=detail,
            actor=actor,
            at=at,
            run_id=reconciliation_run_id,
        )
        return ResumeOutcome.RESUMED

    def events(self, s: Session) -> list[HaltEvent]:
        rows = s.fetch_all(
            "SELECT ts, action, reason_code, detail, actor, reconciliation_run_id "
            "FROM halt_events ORDER BY id"
        )
        return [
            HaltEvent(
                ts=ts_from_db(r[0]),
                action=str_from_db(r[1]),
                reason_code=str_from_db(r[2]),
                detail=opt_str_from_db(r[3]),
                actor=str_from_db(r[4]),
                reconciliation_run_id=opt_int_from_db(r[5]),
            )
            for r in rows
        ]

    @staticmethod
    def _event(
        tx: Tx,
        *,
        action: str,
        reason_code: str,
        detail: str | None,
        actor: str,
        at: datetime,
        run_id: int | None,
    ) -> None:
        tx.insert(
            "INSERT INTO halt_events (ts, action, reason_code, detail, actor, "
            "reconciliation_run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ts_to_db(at), action, reason_code, detail, actor, run_id),
        )
