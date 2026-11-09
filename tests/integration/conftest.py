"""Fixtures for SQLite integration tests. Every database lives under ``tmp_path``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import (
    IntentPurpose,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.repositories import (
    OrderRecord,
    OrdersRepository,
    PortfolioRecord,
    PortfoliosRepository,
)

T0 = datetime(2026, 11, 9, 15, 30, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    database = Database.create(db_path, RuntimeMode.SIMULATE, now=T0)
    yield database
    database.close()


@pytest.fixture
def portfolio_id(db: Database) -> int:
    with db.transaction() as tx:
        return PortfoliosRepository().insert(
            tx,
            PortfolioRecord(
                name="main",
                mode=RuntimeMode.SIMULATE,
                initial_capital=Decimal("5000"),
                max_capital_allocation=Decimal("5000"),
                account_profile="cash_conservative",
                created_at=T0,
            ),
        )


OrderFactory = Callable[..., OrderRecord]


@pytest.fixture
def make_order(db: Database, portfolio_id: int) -> OrderFactory:
    def factory(**overrides: object) -> OrderRecord:
        coid = new_client_order_id()
        rec = OrderRecord(
            client_order_id=coid,
            portfolio_id=portfolio_id,
            intent_key=f"test:{coid}",
            symbol="AAPL",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            purpose=IntentPurpose.ENTRY,
            qty=Decimal("10"),
            limit_price=Decimal("150.25"),
            planned_stop=Decimal("145.00"),
            status=OrderStatus.PENDING_SUBMIT,
            filled_qty=Decimal("0"),
            avg_fill_price=None,
            created_at=T0,
            updated_at=T0,
        )
        rec = replace(rec, **overrides)  # type: ignore[arg-type]
        with db.transaction() as tx:
            OrdersRepository().insert(tx, rec)
        return rec

    return factory
