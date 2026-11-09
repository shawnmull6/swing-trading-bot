"""Helpers for portfolio accounting tests (simulation-mode ledgers under tmp paths)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from swing_trading_bot.domain.enums import (
    IntentPurpose,
    MarkSource,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.domain.models import PriceMark
from swing_trading_bot.domain.valuation import MarkFreshnessPolicy
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.portfolio import (
    CashActivity,
    CashActivityKind,
    ConfirmedFill,
    PortfolioConfig,
    VirtualPortfolio,
)
from swing_trading_bot.ledger.repositories import OrderRecord, OrdersRepository
from tests.support.calendar_fixture import (
    FIXTURE_END,
    FIXTURE_START,
    settlement_fetch,
    trading_fetch,
)

# Monday 2026-11-09 10:00 ET — inside a regular session.
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
CONFIG = PortfolioConfig(
    portfolio_name="main",
    calendar_max_age=timedelta(hours=36),
    marks=MarkFreshnessPolicy.from_seconds(in_session=900, future_skew=5, receipt_age=120),
)
D = Decimal


def seed_calendar(
    db: Database, fetched_at: datetime, start: date = FIXTURE_START, end: date = FIXTURE_END
) -> None:
    with db.transaction() as tx:
        store = CalendarStore()
        assert store.record_fetch(tx, trading_fetch(fetched_at, start, end)).accepted
        assert store.record_fetch(tx, settlement_fetch(fetched_at, start, end)).accepted


def new_ledger(
    path: Path,
    *,
    with_calendar: bool = True,
    calendar_at: datetime = NOW,
    calendar_range: tuple[date, date] = (FIXTURE_START, FIXTURE_END),
    created_at: datetime | None = None,
) -> tuple[Database, VirtualPortfolio]:
    db = Database.create(path, RuntimeMode.SIMULATE, now=NOW)
    with db.transaction() as tx:
        VirtualPortfolio.initialize(
            tx,
            name="main",
            mode=RuntimeMode.SIMULATE,
            initial_capital=D("5000"),
            max_capital_allocation=D("5000"),
            account_profile="cash_conservative",
            at=created_at if created_at is not None else NOW - timedelta(days=30),
        )
    if with_calendar:
        seed_calendar(db, calendar_at - timedelta(minutes=5), *calendar_range)
    return db, VirtualPortfolio(db, CONFIG)


def make_order(
    db: Database,
    *,
    side: Side = Side.BUY,
    qty: str = "10",
    limit: str | None = "150.25",
    symbol: str = "AAPL",
    status: OrderStatus = OrderStatus.PENDING_SUBMIT,
) -> OrderRecord:
    coid = new_client_order_id()
    rec = OrderRecord(
        client_order_id=coid,
        portfolio_id=1,
        intent_key=f"test:{coid}",
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT if limit is not None else OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        purpose=IntentPurpose.ENTRY if side is Side.BUY else IntentPurpose.DISCRETIONARY_EXIT,
        qty=D(qty),
        limit_price=None if limit is None else D(limit),
        planned_stop=D("1.00") if side is Side.BUY else None,
        status=status,
        filled_qty=D("0"),
        avg_fill_price=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with db.transaction() as tx:
        OrdersRepository().insert(tx, rec)
    return rec


def finish(pf: VirtualPortfolio, db: Database, order: OrderRecord, status: OrderStatus) -> Decimal:
    """Record a broker-confirmed terminal status, then close the reservation (one tx)."""
    with db.transaction() as tx:
        current = OrdersRepository().require(tx, order.client_order_id).status
        OrdersRepository().transition_status(
            tx, order.client_order_id, expected=current, target=status, at=NOW
        )
        return pf.close_reservation(tx, order.client_order_id, now=NOW)


def reserve(pf: VirtualPortfolio, db: Database, order: OrderRecord, fee: str = "0") -> Decimal:
    with db.transaction() as tx:
        return pf.reserve_buy(tx, order.client_order_id, fee_estimate=D(fee), now=NOW)


_counter = iter(range(1, 10**9))


def fill(
    order: OrderRecord | None,
    qty: str,
    price: str,
    *,
    fees: str = "0",
    at: datetime = NOW,
    activity_id: str | None = None,
    side: Side | None = None,
    symbol: str | None = None,
    client_order_id: str | None = None,
) -> ConfirmedFill:
    aid = activity_id or f"20261109::fill-{next(_counter)}"
    the_side = side or (order.side if order is not None else Side.BUY)
    return ConfirmedFill(
        activity_id=aid,
        client_order_id=client_order_id or (order.client_order_id if order else None),
        broker_order_id=f"b-{aid}",
        symbol=symbol or (order.symbol if order else "AAPL"),
        side=the_side,
        qty=D(qty),
        price=D(price),
        fees=D(fees),
        transaction_time=at,
        raw_json=json.dumps({"id": aid, "qty": qty, "price": price, "side": the_side.value}),
    )


def cash_activity(
    kind: CashActivityKind, amount: str, *, activity_id: str, at: datetime = NOW
) -> CashActivity:
    return CashActivity(
        activity_id=activity_id,
        kind=kind,
        amount=D(amount),
        occurred_at=at,
        raw_json=json.dumps({"id": activity_id, "net_amount": amount}),
        symbol="AAPL" if kind is CashActivityKind.DIVIDEND else None,
    )


def mark(symbol: str, price: str, *, as_of: datetime, received: datetime | None = None,
         source: MarkSource = MarkSource.LATEST_TRADE) -> PriceMark:  # fmt: skip
    return PriceMark(
        symbol=symbol,
        price=D(price),
        as_of=as_of,
        received_at=received or as_of,
        source=source,
        feed="iex",
    )
