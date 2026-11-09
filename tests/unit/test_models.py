"""Step 3: domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from swing_trading_bot.domain.enums import IntentPurpose, MarkSource, OrderType, Side, TimeInForce
from swing_trading_bot.domain.models import (
    BrokerOrderSnapshot,
    BrokerPosition,
    ExecutionRecord,
    OrderIntent,
    PriceMark,
)

NOW = datetime(2026, 11, 9, 15, 30, tzinfo=UTC)


def _buy(**overrides: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "intent_key": "strat1:AAPL:BUY:2026-11-09",
        "symbol": "AAPL",
        "side": Side.BUY,
        "qty": "5",
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.DAY,
        "limit_price": "150.25",
        "planned_stop": "145.00",
        "purpose": IntentPurpose.ENTRY,
        "created_at": NOW,
    }
    data.update(overrides)
    return OrderIntent.model_validate(data)


def test_valid_buy_and_derived_amounts() -> None:
    intent = _buy()
    assert intent.qty == Decimal(5)
    assert intent.max_notional == Decimal("751.25")
    assert intent.planned_risk == Decimal("26.25")


def test_serialises_decimals_as_strings() -> None:
    dumped = _buy().model_dump(mode="json")
    assert dumped["qty"] == "5"
    assert dumped["limit_price"] == "150.25"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"order_type": OrderType.MARKET, "limit_price": None}, "LIMIT"),
        ({"limit_price": None}, "limit_price"),
        ({"planned_stop": None}, "planned_stop"),
        ({"planned_stop": "150.25"}, "below limit_price"),
        ({"planned_stop": "151"}, "below limit_price"),
        ({"purpose": IntentPurpose.RISK_EXIT}, "ENTRY"),
        ({"qty": "1.5"}, "fractional"),
        ({"qty": "0"}, "positive"),
        ({"qty": 5.0}, "float"),
        ({"limit_price": 150.25}, "float"),
        ({"limit_price": "150.255"}, "precision"),
        ({"symbol": "aapl"}, "pattern"),
        ({"symbol": "TOOLONGX"}, "pattern"),
        ({"intent_key": ""}, "at least 1"),
        ({"created_at": datetime(2026, 11, 9, 15, 30)}, "timezone-aware"),  # noqa: DTZ001
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_invalid_buy(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _buy(**overrides)


def test_sell_rules() -> None:
    sell = _buy(side=Side.SELL, purpose=IntentPurpose.DISCRETIONARY_EXIT, planned_stop=None)
    assert sell.planned_risk is None
    with pytest.raises(ValidationError, match="no shorting"):
        _buy(side=Side.SELL, purpose=IntentPurpose.ENTRY)
    market_sell = _buy(
        side=Side.SELL,
        purpose=IntentPurpose.RISK_EXIT,
        order_type=OrderType.MARKET,
        limit_price=None,
        planned_stop=None,
    )
    assert market_sell.max_notional is None
    with pytest.raises(ValidationError, match="must not carry limit_price"):
        _buy(side=Side.SELL, purpose=IntentPurpose.RISK_EXIT, order_type=OrderType.MARKET)


def test_share_class_symbol_and_timezone_normalisation() -> None:
    eastern = timezone(timedelta(hours=-5))
    intent = _buy(symbol="BRK.B", created_at=datetime(2026, 11, 9, 10, 30, tzinfo=eastern))
    assert intent.symbol == "BRK.B"
    assert intent.created_at == NOW
    assert intent.created_at.tzinfo is UTC


def test_models_are_frozen() -> None:
    intent = _buy()
    with pytest.raises(ValidationError, match="frozen"):
        intent.qty = Decimal(10)  # type: ignore[misc]


def test_price_mark() -> None:
    mark = PriceMark(
        symbol="SPY",
        price=Decimal("590.12"),
        as_of=NOW,
        received_at=NOW + timedelta(seconds=1),
        source=MarkSource.LATEST_TRADE,
        feed="iex",
    )
    assert mark.price == Decimal("590.12")
    with pytest.raises(ValidationError, match="positive"):
        PriceMark(symbol="SPY", price=Decimal(0), as_of=NOW, received_at=NOW, source=MarkSource.SIM)


def test_broker_snapshots_accept_broker_truth() -> None:
    order = BrokerOrderSnapshot(
        client_order_id="stb-x",
        broker_order_id="b-1",
        symbol="AAPL",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        qty=Decimal(5),
        filled_qty=Decimal(2),
        broker_status="partially_filled",
        updated_at=NOW,
    )
    assert order.filled_qty == Decimal(2)
    position = BrokerPosition(symbol="AAPL", qty=Decimal(-3), avg_entry_price=Decimal("10"))
    assert position.qty == Decimal(-3)  # recorded, reconciliation will halt on it
    fill = ExecutionRecord(
        activity_id="20261109153000000::abc",
        broker_order_id="b-1",
        symbol="AAPL",
        side=Side.BUY,
        qty=Decimal(2),
        price=Decimal("150.10"),
        cum_qty=Decimal(2),
        leaves_qty=Decimal(3),
        transaction_time=NOW,
    )
    assert fill.leaves_qty == Decimal(3)


@given(
    qty=st.integers(min_value=1, max_value=10_000),
    limit_cents=st.integers(min_value=1_000, max_value=100_000),
    stop_gap_cents=st.integers(min_value=1, max_value=999),
)
def test_planned_risk_is_exact(qty: int, limit_cents: int, stop_gap_cents: int) -> None:
    limit = Decimal(limit_cents) / 100
    stop = limit - Decimal(stop_gap_cents) / 100
    intent = _buy(qty=str(qty), limit_price=str(limit), planned_stop=str(stop))
    assert intent.planned_risk == Decimal(stop_gap_cents) / 100 * qty
    assert intent.max_notional == limit * qty
