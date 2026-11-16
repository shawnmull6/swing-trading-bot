"""M2-3: derived sell-side share reservations and cancel/replace readiness (pure)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import (
    IntentPurpose,
    OrderRole,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from swing_trading_bot.ledger.repositories import OrderRecord
from swing_trading_bot.oms.share_reservations import (
    ReplaceReadiness,
    SellRefusal,
    SellSideBook,
    held_shares,
    replace_readiness,
)

D = Decimal
S = OrderStatus
R = SellRefusal
T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
PLAN = "tp-" + "a" * 32
OTHER = "tp-" + "b" * 32


def order(
    status: S = S.ACCEPTED,
    qty: str = "6",
    filled: str = "0",
    *,
    side: Side = Side.SELL,
    role: OrderRole | None = OrderRole.TARGET,
    plan: str | None = PLAN,
    broker_filled: str | None = None,
    coid: str = "stb-1",
    symbol: str = "MSFT",
) -> OrderRecord:
    return OrderRecord(
        client_order_id=coid, portfolio_id=1, intent_key=f"k-{coid}", symbol=symbol, side=side,
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY,
        purpose=IntentPurpose.ENTRY if side is Side.BUY else IntentPurpose.DISCRETIONARY_EXIT,
        qty=D(qty), limit_price=D("54.00"), planned_stop=None, status=status,
        filled_qty=D(filled), avg_fill_price=None, created_at=T0, updated_at=T0,
        trade_plan_id=plan, order_role=role,
        broker_filled_qty=None if broker_filled is None else D(broker_filled),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("status", "held"),
    [(S.PENDING_SUBMIT, 6), (S.SUBMIT_AMBIGUOUS, 6), (S.ACCEPTED, 6),
     (S.PARTIALLY_FILLED, 4), (S.PENDING_CANCEL, 4),  # a cancel request frees nothing
     (S.FILLED, 0), (S.CANCELED, 0), (S.EXPIRED, 0), (S.REJECTED, 0),
     (S.CONFIRMED_ABSENT, 0), (S.ORPHAN, 0)],
)  # fmt: skip
def test_held_shares_by_status(status: S, held: int) -> None:
    filled = "2" if status in (S.PARTIALLY_FILLED, S.PENDING_CANCEL) else "0"
    assert held_shares(order(status, filled=filled)) == held


def test_buys_hold_no_shares() -> None:
    assert held_shares(order(side=Side.BUY, role=OrderRole.ENTRY)) == 0


def test_book_reservation_and_availability() -> None:
    orders = [order(S.PARTIALLY_FILLED, filled="2"), order(S.ACCEPTED, symbol="AAPL", coid="x")]
    book = SellSideBook.build("MSFT", D(12), orders)
    assert (book.reserved, book.available) == (D(4), D(8))
    assert book.check_new_sell(D(8), trade_plan_id=PLAN, role=OrderRole.TARGET) == ()
    assert book.check_new_sell(D(9), trade_plan_id=PLAN, role=OrderRole.TARGET) == (
        R.INSUFFICIENT_SHARES,
    )


def test_stop_and_target_competing_for_the_same_shares() -> None:
    target = order(S.ACCEPTED, "6", role=OrderRole.TARGET)
    book = SellSideBook.build("MSFT", D(12), [target])
    # A protective stop for the whole position would oversell: target 6 + stop 12 > 12.
    assert R.INSUFFICIENT_SHARES in book.check_new_sell(
        D(12), trade_plan_id=PLAN, role=OrderRole.PROTECTIVE_STOP
    )
    assert book.check_new_sell(D(6), trade_plan_id=PLAN, role=OrderRole.PROTECTIVE_STOP) == ()


def test_pending_cancel_and_ambiguous_holds_are_uncertain() -> None:
    for status in (S.PENDING_CANCEL, S.SUBMIT_AMBIGUOUS, S.PENDING_SUBMIT):
        book = SellSideBook.build("MSFT", D(6), [order(status)])
        assert book.uncertain and book.available == 0
        assert R.INSUFFICIENT_SHARES in book.check_new_sell(
            D(1), trade_plan_id=PLAN, role=OrderRole.EXIT
        )


def test_unbooked_broker_fills_keep_shares_held_and_refuse() -> None:
    o = order(S.PARTIALLY_FILLED, filled="2", broker_filled="5")
    book = SellSideBook.build("MSFT", D(12), [o])
    assert book.reserved == 4  # the ledger still holds the 3 unbooked shares
    assert R.UNBOOKED_FILLS in book.check_new_sell(D(1), trade_plan_id=PLAN, role=OrderRole.EXIT)


def test_plan_conflicts_roles_and_working_entries() -> None:
    other = order(plan=OTHER)
    unplanned = order(plan=None, role=None, coid="m1")
    assert R.PLAN_CONFLICT in SellSideBook.build("MSFT", D(12), [other]).check_new_sell(
        D(1), trade_plan_id=PLAN, role=OrderRole.TARGET
    )
    assert R.PLAN_CONFLICT in SellSideBook.build("MSFT", D(12), [unplanned]).check_new_sell(
        D(1), trade_plan_id=PLAN, role=OrderRole.TARGET
    )
    stop = order(role=OrderRole.EXIT, qty="1")
    assert R.ROLE_ALREADY_LIVE in SellSideBook.build("MSFT", D(12), [stop]).check_new_sell(
        D(1), trade_plan_id=PLAN, role=OrderRole.EXIT
    )
    # several TARGET orders are allowed by the book (scale-outs, §9)
    assert (
        SellSideBook.build("MSFT", D(12), [order()]).check_new_sell(
            D(1), trade_plan_id=PLAN, role=OrderRole.TARGET
        )
        == ()
    )
    entry = order(side=Side.BUY, role=OrderRole.ENTRY, coid="e")
    assert R.ENTRY_STILL_WORKING in SellSideBook.build("MSFT", D(5), [entry]).check_new_sell(
        D(1), trade_plan_id=PLAN, role=OrderRole.TARGET
    )
    bad = SellSideBook.build("MSFT", D(5), []).check_new_sell(
        D("1.5"), trade_plan_id=PLAN, role=OrderRole.ENTRY
    )
    assert set(bad) == {R.INVALID_QTY, R.INVALID_ROLE}


def test_violations_detect_an_oversold_book() -> None:
    book = SellSideBook.build("MSFT", D(5), [order(qty="6")])  # e.g. after a reconciliation
    assert book.violations() and R.BOOK_INCONSISTENT in book.check_new_sell(
        D(1), trade_plan_id=PLAN, role=OrderRole.TARGET
    )
    two_stops = [order(role=OrderRole.PROTECTIVE_STOP, qty="1", coid=c) for c in ("a", "b")]
    violations = SellSideBook.build("MSFT", D(5), two_stops).violations()
    assert any("PROTECTIVE_STOP" in v for v in violations)
    assert SellSideBook.build("MSFT", D(-1), []).violations()


@pytest.mark.parametrize(
    ("status", "broker_filled", "expected"),
    [(S.ACCEPTED, None, ReplaceReadiness.CANCEL_NOT_REQUESTED),
     (S.PARTIALLY_FILLED, None, ReplaceReadiness.CANCEL_NOT_REQUESTED),
     (S.PENDING_CANCEL, None, ReplaceReadiness.AWAITING_CANCEL_CONFIRMATION),
     (S.PENDING_CANCEL, "4", ReplaceReadiness.AWAITING_FILL_BOOKING),
     (S.SUBMIT_AMBIGUOUS, None, ReplaceReadiness.OLD_ORDER_UNRESOLVED),
     (S.PENDING_SUBMIT, None, ReplaceReadiness.OLD_ORDER_UNRESOLVED),
     (S.ORPHAN, None, ReplaceReadiness.OLD_ORDER_UNRESOLVED),
     (S.FILLED, None, ReplaceReadiness.OLD_ORDER_FILLED),
     (S.CANCELED, None, ReplaceReadiness.READY),
     (S.EXPIRED, None, ReplaceReadiness.READY),
     (S.CONFIRMED_ABSENT, None, ReplaceReadiness.READY)],
)  # fmt: skip
def test_replace_readiness(
    status: S, broker_filled: str | None, expected: ReplaceReadiness
) -> None:
    assert replace_readiness(order(status, broker_filled=broker_filled)) is expected


_STATUSES = st.sampled_from(list(S))


@given(
    position=st.integers(min_value=0, max_value=50),
    specs=st.lists(st.tuples(_STATUSES, st.integers(1, 20), st.integers(0, 20)), max_size=6),
    new_qty=st.integers(min_value=1, max_value=60),
)
def test_book_never_permits_an_oversell(
    position: int, specs: list[tuple[S, int, int]], new_qty: int
) -> None:
    orders = [
        order(status, str(qty), str(min(filled, qty)), coid=f"o{i}")
        for i, (status, qty, filled) in enumerate(specs)
    ]
    book = SellSideBook.build("MSFT", D(position), orders)
    if not book.check_new_sell(D(new_qty), trade_plan_id=PLAN, role=OrderRole.TARGET):
        assert book.reserved + new_qty <= position
    assert book.reserved == sum((held_shares(o) for o in orders), D(0))


def test_terminal_sell_with_unbooked_fills_keeps_them_held() -> None:
    """Defence in depth: the lifecycle never closes an order with unbooked fills, but if
    one were terminal, its broker-sold shares stay held (no premature release)."""
    o = order(S.CANCELED, qty="6", filled="2", broker_filled="5")
    assert held_shares(o) == 3
    book = SellSideBook.build("MSFT", D(10), [o])
    assert book.reserved == 3 and book.uncertain
    assert R.UNBOOKED_FILLS in book.check_new_sell(D(1), trade_plan_id=PLAN, role=OrderRole.EXIT)
    done = order(S.CANCELED, qty="6", filled="5", broker_filled="5")
    assert held_shares(done) == 0 and SellSideBook.build("MSFT", D(7), [done]).holds == ()


def test_terminal_buys_never_count_as_working_entries() -> None:
    buy = order(S.CANCELED, side=Side.BUY, role=OrderRole.ENTRY, filled="2", broker_filled="3")
    book = SellSideBook.build("MSFT", D(2), [buy])
    assert book.working_buys == () and book.unbooked == ("stb-1",)


def test_a_confirmed_absent_sell_that_reappears_holds_again() -> None:
    """CONFIRMED_ABSENT releases the hold; adoption (a legal, halting transition) puts
    the order back to a working status, which holds its shares again."""
    assert held_shares(order(S.CONFIRMED_ABSENT)) == 0
    assert held_shares(order(S.ACCEPTED)) == 6
