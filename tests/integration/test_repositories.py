"""Step 4: repositories — round trips, idempotent ingestion, halts, invariants."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import (
    ActivityAttribution,
    HaltReason,
    MarkSource,
    OrderRole,
    OrderStatus,
    ReconciliationResult,
    RuntimeMode,
    Side,
)
from swing_trading_bot.domain.models import PriceMark
from swing_trading_bot.domain.state_machine import IllegalTransitionError
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import (
    ConcurrencyConflictError,
    ConstraintViolationError,
    PersistenceError,
    RecordNotFoundError,
)
from swing_trading_bot.ledger.invariants import (
    InvariantChecker,
    Violation,
    default_checker,
)
from swing_trading_bot.ledger.repositories import (
    ActivitiesRepository,
    ActivityRecord,
    CashEntriesRepository,
    CashEntryKind,
    CashEntryRecord,
    FillRecord,
    FillsRepository,
    IngestResult,
    OrderEvent,
    OrderEventsRepository,
    OrderProgress,
    OrdersRepository,
    PortfoliosRepository,
    PositionRecord,
    PositionsRepository,
    PriceMarksRepository,
    ReconciliationRunsRepository,
    ReservationsRepository,
    ReservationStatus,
    ResumeOutcome,
    TradingStateRepository,
    canonical_json,
)
from swing_trading_bot.observability.logging import configure_logging

from .conftest import T0, OrderFactory, at

orders = OrdersRepository()
activities = ActivitiesRepository()
fills = FillsRepository()
reservations = ReservationsRepository()
cash = CashEntriesRepository()
positions = PositionsRepository()
state = TradingStateRepository()
runs = ReconciliationRunsRepository()


def _activity(activity_id: str, client_order_id: str | None, **overrides: object) -> ActivityRecord:
    raw = {"id": activity_id, "activity_type": "FILL", "qty": "4", "price": "150.10"}
    rec = ActivityRecord(
        activity_id=activity_id,
        activity_type="FILL",
        ts=at(1),
        attribution=ActivityAttribution.ATTRIBUTABLE,
        raw_json=json.dumps(raw),
        ingested_at=at(2),
        symbol="AAPL",
        qty=Decimal("4"),
        price=Decimal("150.10"),
        net_amount=Decimal("-600.40"),
        side=Side.BUY,
        broker_order_id="b-1",
        client_order_id=client_order_id,
    )
    return replace(rec, **overrides)  # type: ignore[arg-type]


def _fill(activity_id: str, client_order_id: str, qty: str = "4") -> FillRecord:
    return FillRecord(
        activity_id=activity_id,
        client_order_id=client_order_id,
        symbol="AAPL",
        side=Side.BUY,
        qty=Decimal(qty),
        price=Decimal("150.10"),
        fees=Decimal("0"),
        filled_at=at(1),
        recorded_at=at(2),
    )


# ------------------------------------------------------------------- portfolios


def test_portfolio_round_trip(db: Database, portfolio_id: int) -> None:
    with db.read() as s:
        p = PortfoliosRepository().get(s, portfolio_id)
        assert PortfoliosRepository().get_by_name(s, "main") == p
        assert PortfoliosRepository().get_by_name(s, "nope") is None
        with pytest.raises(RecordNotFoundError):
            PortfoliosRepository().get(s, 999)
    assert p.initial_capital == Decimal("5000")
    assert p.mode is RuntimeMode.SIMULATE
    assert p.created_at == T0


# ----------------------------------------------------------------------- orders


def test_order_round_trip(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    with db.read() as s:
        assert orders.get(s, rec.client_order_id) == rec
        assert orders.get(s, "stb-unknown") is None
        assert [o.client_order_id for o in orders.list_working(s)] == [rec.client_order_id]
        assert orders.list_by_status(s, OrderStatus.PENDING_SUBMIT) == [rec]
        with pytest.raises(RecordNotFoundError):
            orders.require(s, "stb-unknown")


def test_reserved_rth01_fields_rejected_in_m1(db: Database, make_order: OrderFactory) -> None:
    with pytest.raises(PersistenceError, match="RTH-01"):
        make_order(order_role=OrderRole.ENTRY)
    with pytest.raises(PersistenceError, match="RTH-01"):
        make_order(trade_plan_id="plan-1")


def test_order_lifecycle_with_validated_transitions(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    coid = rec.client_order_id
    with db.transaction() as tx:
        orders.record_send_attempt(tx, coid, at(1))
    with db.transaction() as tx, pytest.raises(ConcurrencyConflictError):
        orders.record_send_attempt(tx, coid, at(2))
    with db.transaction() as tx:
        t = orders.transition_status(
            tx, coid, expected=OrderStatus.PENDING_SUBMIT, target=OrderStatus.ACCEPTED, at=at(2)
        )
        orders.set_broker_order_id(tx, coid, "b-1", at(2))
    assert not t.releases_reservation
    with db.transaction() as tx:
        orders.transition_status(
            tx,
            coid,
            expected=OrderStatus.ACCEPTED,
            target=OrderStatus.PARTIALLY_FILLED,
            at=at(3),
            progress=OrderProgress(
                filled_qty=Decimal("4"),
                avg_fill_price=Decimal("150.10"),
                last_broker_status="partially_filled",
                last_synced_at=at(3),
            ),
        )
    with db.read() as s:
        stored = orders.require(s, coid)
        assert orders.get_by_broker_id(s, "b-1") == stored
    assert stored.status is OrderStatus.PARTIALLY_FILLED
    assert stored.filled_qty == Decimal("4")
    assert stored.send_attempted_at == at(1)
    assert stored.last_synced_at == at(3)


def test_illegal_transition_rejected_before_write(db: Database, make_order: OrderFactory) -> None:
    rec = make_order(status=OrderStatus.FILLED, filled_qty=Decimal("10"))
    with db.transaction() as tx, pytest.raises(IllegalTransitionError):
        orders.transition_status(
            tx, rec.client_order_id, expected=OrderStatus.FILLED,
            target=OrderStatus.CANCELED, at=at(1),
        )  # fmt: skip


def test_optimistic_status_check(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    with db.transaction() as tx, pytest.raises(ConcurrencyConflictError):
        orders.transition_status(
            tx, rec.client_order_id, expected=OrderStatus.ACCEPTED,
            target=OrderStatus.FILLED, at=at(1),
        )  # fmt: skip
    with db.transaction() as tx, pytest.raises(RecordNotFoundError):
        orders.set_broker_order_id(tx, "stb-missing", "b-9", at(1))


def test_order_events(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    events = OrderEventsRepository()
    with db.transaction() as tx:
        events.append(
            tx,
            OrderEvent(
                client_order_id=rec.client_order_id,
                ts=at(1),
                source="LOCAL",
                from_status=OrderStatus.PENDING_SUBMIT,
                to_status=OrderStatus.ACCEPTED,
                detail={"amount": Decimal("1.50"), "note": "x"},
            ),
        )
        events.append(tx, OrderEvent(rec.client_order_id, at(2), "BROKER"))
    with db.read() as s:
        stored = events.list_for_order(s, rec.client_order_id)
    assert stored[0].detail == {"amount": "1.50", "note": "x"}
    assert stored[1].from_status is None


# ----------------------------------------------------------------- reservations


def test_reservation_lifecycle(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    coid = rec.client_order_id
    with db.transaction() as tx:
        reservations.create(tx, coid, Decimal("1502.5000"), at(0))
        reservations.set_remaining(tx, coid, Decimal("901.5000"), at(1))
    with db.read() as s:
        r = reservations.get(s, coid)
        assert r is not None and r.remaining == Decimal("901.5000")
        assert r.original == Decimal("1502.5000")
        assert reservations.list_active(s) == [r]
    with db.transaction() as tx:
        reservations.close(tx, coid, ReservationStatus.RELEASED, at(2))
    with db.transaction() as tx, pytest.raises(ConcurrencyConflictError):
        reservations.set_remaining(tx, coid, Decimal("1"), at(3))
    with db.transaction() as tx, pytest.raises(ConcurrencyConflictError):
        reservations.close(tx, coid, ReservationStatus.CONSUMED, at(3))
    with db.transaction() as tx, pytest.raises(ValueError, match="RELEASED or CONSUMED"):
        reservations.close(tx, coid, ReservationStatus.ACTIVE, at(3))
    with db.read() as s:
        assert reservations.list_active(s) == []
        assert reservations.get(s, "stb-none") is None


# ------------------------------------------------------- idempotent ingestion


def test_activity_ingestion_is_idempotent(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    a = _activity("20261109153100000::a1", rec.client_order_id)
    with db.transaction() as tx:
        assert activities.record(tx, a) is IngestResult.INSERTED
    # Replays (even with a later ingestion time / reformatted JSON) are no-ops.
    replay = replace(a, ingested_at=at(9), raw_json=json.dumps(json.loads(a.raw_json), indent=2))
    for _ in range(3):
        with db.transaction() as tx:
            assert activities.record(tx, replay) is IngestResult.DUPLICATE
    with db.read() as s:
        assert activities.count(s) == 1
        assert activities.get(s, a.activity_id) == a


@pytest.mark.parametrize(
    "change",
    [{"qty": Decimal("5")}, {"price": Decimal("150.11")}, {"side": Side.SELL},
     {"raw_json": '{"id": "x"}'}, {"ts": at(7)}],
)  # fmt: skip
def test_activity_id_with_different_content_is_conflict(
    db: Database, make_order: OrderFactory, change: dict[str, object]
) -> None:
    rec = make_order()
    a = _activity("act-1", rec.client_order_id)
    with db.transaction() as tx:
        activities.record(tx, a)
    with db.transaction() as tx:
        assert activities.record(tx, replace(a, **change)) is IngestResult.CONFLICT  # type: ignore[arg-type]
    with db.read() as s:
        assert activities.get(s, "act-1") == a  # stored content never overwritten


def test_fill_ingestion_is_idempotent(db: Database, make_order: OrderFactory) -> None:
    rec = make_order()
    with db.transaction() as tx:
        activities.record(tx, _activity("act-1", rec.client_order_id))
        result, fill_id = fills.insert(tx, _fill("act-1", rec.client_order_id))
    assert result is IngestResult.INSERTED
    with db.transaction() as tx:
        replay = replace(_fill("act-1", rec.client_order_id), recorded_at=at(9))
        again, again_id = fills.insert(tx, replay)
        conflict, _ = fills.insert(tx, _fill("act-1", rec.client_order_id, qty="5"))
    assert (again, again_id) == (IngestResult.DUPLICATE, fill_id)
    assert conflict is IngestResult.CONFLICT
    with db.read() as s:
        assert len(fills.list_for_order(s, rec.client_order_id)) == 1
        assert fills.get_by_activity(s, "act-1") is not None


def test_fill_requires_ingested_activity_and_known_order(
    db: Database, make_order: OrderFactory
) -> None:
    rec = make_order()
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"), db.transaction() as tx:
        fills.insert(tx, _fill("never-ingested", rec.client_order_id))
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"), db.transaction() as tx:
        activities.record(tx, _activity("act-x", None))
        fills.insert(tx, _fill("act-x", "stb-unknown-order"))


def test_broker_truth_violating_invariants_is_recorded_then_reported(
    db: Database, make_order: OrderFactory, portfolio_id: int
) -> None:
    """A fill larger than the order is broker truth: write succeeds, checker reports."""
    rec = make_order(qty=Decimal("10"))
    with db.transaction() as tx:
        activities.record(tx, _activity("act-big", rec.client_order_id, qty=Decimal("12")))
        result, fill_id = fills.insert(tx, _fill("act-big", rec.client_order_id, qty="12"))
        orders.transition_status(
            tx, rec.client_order_id, expected=OrderStatus.PENDING_SUBMIT,
            target=OrderStatus.ACCEPTED, at=at(1),
            progress=OrderProgress(filled_qty=Decimal("12")),
        )  # fmt: skip
        cash.append(
            tx,
            CashEntryRecord(
                portfolio_id=portfolio_id, ts=at(1), kind=CashEntryKind.BUY,
                amount=Decimal("-99999"), trade_date=date(2026, 11, 9), settles_on=None,
                activity_id="act-big", fill_id=fill_id,
            ),
        )  # fmt: skip
        positions.upsert(
            tx,
            PositionRecord(
                portfolio_id=portfolio_id, symbol="MSFT", qty=Decimal("-3"),
                cost_basis=Decimal("0"), realized_pnl=Decimal("0"), updated_at=at(1),
            ),
        )  # fmt: skip
    assert result is IngestResult.INSERTED
    with db.read() as s:
        report = default_checker().run(s)
    names = {v.invariant for v in report.violations}
    assert not report.ok
    assert {"order_fill_consistency", "position_qty_replay", "non_negative_positions"} <= names


def test_clean_ledger_passes_invariants(
    db: Database, make_order: OrderFactory, portfolio_id: int
) -> None:
    rec = make_order()
    with db.transaction() as tx:
        reservations.create(tx, rec.client_order_id, Decimal("1502.50"), at(0))
        activities.record(tx, _activity("act-1", rec.client_order_id))
        fills.insert(tx, _fill("act-1", rec.client_order_id))
        orders.transition_status(
            tx, rec.client_order_id, expected=OrderStatus.PENDING_SUBMIT,
            target=OrderStatus.PARTIALLY_FILLED, at=at(1),
            progress=OrderProgress(filled_qty=Decimal("4")),
        )  # fmt: skip
        positions.upsert(
            tx,
            PositionRecord(
                portfolio_id=portfolio_id, symbol="AAPL", qty=Decimal("4"),
                cost_basis=Decimal("600.40"), realized_pnl=Decimal("0"), updated_at=at(1),
                opened_at=at(1), opened_session=date(2026, 11, 9),
            ),
        )  # fmt: skip
    with db.read() as s:
        report = default_checker().run(s)
        assert positions.get(s, portfolio_id, "AAPL") is not None
        assert len(positions.list_for_portfolio(s, portfolio_id)) == 1
    assert report.ok, report.violations
    assert "reservation_consistency" in report.checked


def test_reservation_invariant_detects_mismatch(db: Database, make_order: OrderFactory) -> None:
    done = make_order(status=OrderStatus.CANCELED)
    working = make_order(status=OrderStatus.ACCEPTED)
    with db.transaction() as tx:
        reservations.create(tx, done.client_order_id, Decimal("10"), at())
        reservations.create(tx, working.client_order_id, Decimal("10"), at())
        reservations.close(tx, working.client_order_id, ReservationStatus.RELEASED, at())
    with db.read() as s:
        report = default_checker().run(s)
    subjects = {v.subject for v in report.violations if v.invariant == "reservation_consistency"}
    assert subjects == {done.client_order_id, working.client_order_id}


def test_checker_contains_failing_invariants() -> None:
    class Broken:
        name = "broken"

        def check(self, s: object) -> list[Violation]:
            raise RuntimeError("boom")

    checker = InvariantChecker([Broken()])
    with pytest.raises(ValueError, match="unique"):
        checker.with_additional([Broken()])
    report = checker.run(None)  # type: ignore[arg-type]
    assert report.violations == (Violation("broken", "checker", "check raised RuntimeError"),)


# ------------------------------------------------------------------------ cash


def test_cash_entries_and_settlement_resolution(db: Database, portfolio_id: int) -> None:
    entry = CashEntryRecord(
        portfolio_id=portfolio_id, ts=at(0), kind=CashEntryKind.DEPOSIT,
        amount=Decimal("5000.00"), trade_date=date(2026, 11, 9), settles_on=None, memo="seed",
    )  # fmt: skip
    with db.transaction() as tx:
        entry_id = cash.append(tx, entry)
        cash.resolve_settlement(tx, entry_id, date(2026, 11, 9))
    with db.transaction() as tx, pytest.raises(ConcurrencyConflictError):
        cash.resolve_settlement(tx, entry_id, date(2026, 11, 10))
    with db.read() as s:
        (stored,) = cash.list_for_portfolio(s, portfolio_id)
    assert stored == replace(entry, id=entry_id, settles_on=date(2026, 11, 9))
    assert str(stored.amount) == "5000.00"


def test_duplicate_cash_effect_for_same_activity_rejected(
    db: Database, make_order: OrderFactory, portfolio_id: int
) -> None:
    rec = make_order()
    fee = CashEntryRecord(
        portfolio_id=portfolio_id, ts=at(1), kind=CashEntryKind.FEE, amount=Decimal("-0.02"),
        trade_date=date(2026, 11, 9), settles_on=date(2026, 11, 10), activity_id="act-fee",
    )  # fmt: skip
    with db.transaction() as tx:
        activities.record(tx, _activity("act-fee", rec.client_order_id, activity_type="FEE"))
        cash.append(tx, fee)
    with pytest.raises(ConstraintViolationError, match="UNIQUE"), db.transaction() as tx:
        cash.append(tx, fee)


amounts = st.decimals(
    min_value=Decimal("-1e12"), max_value=Decimal("1e12"), places=4, allow_nan=False
)


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(amount=amounts, qty=st.integers(min_value=-(10**9), max_value=10**9))
def test_decimal_round_trip_through_sqlite(
    db: Database, portfolio_id: int, amount: Decimal, qty: int
) -> None:
    symbol = "RT"
    with db.transaction() as tx:
        positions.upsert(
            tx,
            PositionRecord(
                portfolio_id=portfolio_id, symbol=symbol, qty=Decimal(qty), cost_basis=amount,
                realized_pnl=-amount, updated_at=at(),
            ),
        )  # fmt: skip
    with db.read() as s:
        stored = positions.get(s, portfolio_id, symbol)
        raw = s.fetch_one(
            "SELECT typeof(qty), typeof(cost_basis) FROM positions WHERE symbol = ?", (symbol,)
        )
    assert stored is not None
    assert stored.cost_basis == amount
    assert stored.realized_pnl == -amount
    assert stored.qty == Decimal(qty)
    assert raw == ("text", "text")


# ------------------------------------------------------------- marks and runs


def test_price_mark_upsert(db: Database) -> None:
    marks = PriceMarksRepository()
    first = PriceMark(symbol="SPY", price=Decimal("590.12"), as_of=at(0), received_at=at(1),
                      source=MarkSource.LATEST_TRADE, feed="iex")  # fmt: skip
    second = first.model_copy(update={"price": Decimal("591.00"), "as_of": at(5)})
    with db.transaction() as tx:
        marks.upsert(tx, first)
        marks.upsert(tx, second)
    with db.read() as s:
        assert marks.get(s, "SPY") == second
        assert marks.get(s, "QQQ") is None


# ------------------------------------------------------- halts across restart


def test_halt_state_persists_across_restart(db: Database, db_path: Path) -> None:
    with db.transaction() as tx:
        state.halt(tx, HaltReason.RECON_POSITION, detail="extra AAPL at broker",
                   actor="reconciler", at=at(1))  # fmt: skip
    db.close()
    with Database.open(db_path, RuntimeMode.SIMULATE) as again, again.read() as s:
        current = state.get(s)
        actions = [(e.action, e.reason_code) for e in state.events(s)]
    assert current.halted
    assert current.reason_code == "RECON_POSITION"
    assert actions == [("HALT", "INITIALIZED"), ("HALT", "RECON_POSITION")]


def test_resume_requires_ok_reconciliation_and_audits_refusals(db: Database, db_path: Path) -> None:
    with db.transaction() as tx:
        bad_run = runs.append(tx, ReconciliationResult.MISMATCH, {"positions": ["AAPL"]}, at(1))
        outcome = state.resume(tx, detail="try", actor="operator", at=at(2),
                               reconciliation_run_id=bad_run)  # fmt: skip
    assert outcome is ResumeOutcome.REFUSED
    with db.transaction() as tx:
        missing = state.resume(tx, detail="try", actor="operator", at=at(3),
                               reconciliation_run_id=9999)  # fmt: skip
    assert missing is ResumeOutcome.REFUSED
    with db.read() as s:
        assert state.get(s).halted
    with db.transaction() as tx:
        ok_run = runs.append(tx, ReconciliationResult.OK, None, at(4))
        assert state.resume(tx, detail="clean recon", actor="operator", at=at(5),
                            reconciliation_run_id=ok_run) is ResumeOutcome.RESUMED  # fmt: skip
    db.close()
    with Database.open(db_path, RuntimeMode.SIMULATE) as again, again.read() as s:
        assert not state.get(s).halted
        assert [e.action for e in state.events(s)] == [
            "HALT", "RESUME_REFUSED", "RESUME_REFUSED", "RESUME",
        ]  # fmt: skip
        latest = runs.latest(s)
        assert latest is not None and latest.result is ReconciliationResult.OK
        first = runs.get(s, bad_run)
        assert first is not None and first.diff == {"positions": ["AAPL"]}


def test_canonical_json_is_deterministic() -> None:
    assert canonical_json({"b": 1, "a": Decimal("1.10")}) == '{"a":"1.10","b":1}'
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


# ------------------------------------------------- no secrets in error logging


def test_database_errors_logged_without_values(db: Database, portfolio_id: int) -> None:
    secret = "PKFAKESECRETVALUE-987654321"
    stream = io.StringIO()
    logger = logging.getLogger("test.db.errors")
    logger.propagate = False
    configure_logging(secrets=[], stream=stream, logger=logger)
    try:
        with db.transaction() as tx:
            cash.append(
                tx,
                CashEntryRecord(
                    portfolio_id=portfolio_id + 100, ts=at(), kind=CashEntryKind.DEPOSIT,
                    amount=Decimal("1"), trade_date=date(2026, 11, 9),
                    settles_on=date(2026, 11, 9), memo=secret,
                ),
            )  # fmt: skip
    except PersistenceError:
        logger.exception("ledger write failed")
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logging.captureWarnings(capture=False)
    output = stream.getvalue()
    assert "FOREIGN KEY" in output
    assert secret not in output
