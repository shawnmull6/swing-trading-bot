"""Step 16.9: irreversible loss events need a fresh broker-vs-ledger quantity check.

Driven through the real ``sync_cycle`` on a simulation ledger (SimBroker): an overnight
split shows up as broker quantity != ledger quantity together with a post-split price.
Before Step 16.9 the cycle valued the stale ledger quantity at the new price and wrote a
permanent lockout / weekly halt. Now the cycle verifies positions first; on mismatch it
writes no loss event, raises the existing RECON_POSITION halt and blocks entries, while a
genuine crash with matching quantities is still recorded exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.app.paths import DataPaths
from swing_trading_bot.app.runtime import CycleReport, record_marks, verify_positions
from swing_trading_bot.app.simulation import SYMBOL, Harness, bring_up, create_harness
from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.domain.enums import HaltReason, OrderStatus, Side
from swing_trading_bot.domain.models import BrokerPosition
from swing_trading_bot.ledger.errors import NestedTransactionError
from swing_trading_bot.ledger.loss_limit_store import LossEventKind, LossLimitEventsRepository
from swing_trading_bot.ledger.loss_limits import LossBlocker
from swing_trading_bot.ledger.position_verification import (
    PositionVerification,
    VerificationStatus,
    compare_positions,
    ledger_quantities,
)
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.risk.gates import GateReason
from tests.support.cli_env import CONFIG_DIR
from tests.support.portfolio import fill, finish, make_order, reserve

D = Decimal


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    (tmp_path / "data").mkdir()
    paths = DataPaths.at(tmp_path / "data")
    paths.sim_dir.mkdir()
    harness = create_harness(paths, "safety", AppConfig.load(CONFIG_DIR))
    yield harness
    harness.db.close()


def holding(h: Harness) -> str:
    """10 SIMA filled at 50.00 (inception baseline 5000, position ~10% of equity)."""
    bring_up(h)
    placed = h.place(qty="10", entry="50.00", stop="48.00")
    assert placed.status is OrderStatus.ACCEPTED and placed.client_order_id
    h.sim.schedule_fill(placed.client_order_id, D(10), D("50.00"), after=timedelta(seconds=30))
    h.advance(timedelta(minutes=1))
    report = h.cycle("fill")
    assert h.position() == D(10)
    assert report.positions is not None and report.positions.verified
    return placed.client_order_id


def price(h: Harness, value: str) -> None:
    h.prices[SYMBOL] = D(value)
    h.sim.set_price(SYMBOL, D(value))


def broker_split(h: Harness, ratio: Decimal) -> None:
    """The broker now reports ``ratio`` x the shares (once); the ledger has not booked it."""

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        return {**raw, "qty": format(D(raw["qty"]) * ratio, "f")}

    h.sim.corrupt_next("get_positions", mutate)


def loss_events(h: Harness) -> dict[LossEventKind, int]:
    with h.db.read() as s:
        return {k: len(LossLimitEventsRepository().list(s, 1, k)) for k in LossEventKind}


def halts(h: Harness) -> list[str]:
    with h.db.read() as s:
        return [e.reason_code for e in TradingStateRepository().events(s) if e.action == "HALT"]


def gate_codes(h: Harness) -> set[str]:
    return set(h.rt.gates.evaluate(now=h.now()).reason_codes)


def cycle_after(h: Harness, minutes: int = 1) -> CycleReport:
    h.advance(timedelta(minutes=minutes))
    return h.cycle("cycle")


# ============================================================ verified quantities
def test_normal_cycle_verifies_and_records_nothing_spurious(h: Harness) -> None:
    holding(h)
    report = cycle_after(h)
    assert report.positions is not None
    assert report.positions.status is VerificationStatus.VERIFIED
    assert report.positions.ledger_qty == {SYMBOL: D(10)} == report.positions.broker_qty
    assert report.loss is not None and report.loss.loss_events_deferred is None
    assert sum(loss_events(h).values()) == 0
    assert not {"POSITION_QTY_UNVERIFIED", "POSITION_QTY_MISMATCH"} & gate_codes(h)


@pytest.mark.parametrize(
    ("crash_to", "lockout", "weekly"),
    [("30.00", 1, 0), ("20.00", 1, 1)],  # -200 (4%): lockout; -300 (6%): + weekly halt
)
def test_genuine_crash_with_matching_quantities_is_recorded(
    h: Harness, crash_to: str, lockout: int, weekly: int
) -> None:
    holding(h)
    price(h, crash_to)
    report = cycle_after(h)
    assert report.positions is not None and report.positions.verified
    assert report.loss is not None and report.loss.loss_events_deferred is None
    events = loss_events(h)
    assert (events[LossEventKind.DAILY_LOCKOUT], events[LossEventKind.WEEKLY_HALT]) == (
        lockout, weekly)  # fmt: skip
    assert "DAILY_LOSS_LOCKOUT" in gate_codes(h)
    assert (HaltReason.WEEKLY_DRAWDOWN.value in halts(h)) is bool(weekly)
    # inception baseline (no reference close): nothing to compare a discontinuity with;
    # discontinuity diagnostics are covered in test_reference_closes.py
    assert report.loss.discontinuities == ()


# ============================================================ splits (quantity mismatch)
@pytest.mark.parametrize("ratio", [D(10), D(2), D("1.5")], ids=["10-for-1", "2-for-1", "3-for-2"])
def test_overnight_split_never_records_a_false_loss_event(h: Harness, ratio: Decimal) -> None:
    coid = holding(h)
    price(h, format(D("50.00") / ratio, "f")[:6])
    broker_split(h, ratio)
    report = cycle_after(h)
    assert report.positions is not None
    assert report.positions.status is VerificationStatus.MISMATCH
    assert report.positions.broker_qty == {SYMBOL: D(10) * ratio}
    assert report.loss is not None and report.loss.loss_events_deferred is not None
    assert sum(loss_events(h).values()) == 0  # nothing irreversible from unverified qty
    assert HaltReason.RECON_POSITION.value in report.halts_raised  # existing safety halt
    assert HaltReason.WEEKLY_DRAWDOWN.value not in halts(h)
    codes = gate_codes(h)
    assert {"POSITION_QTY_MISMATCH", "PERSISTENT_HALT"} <= codes
    assert not h.rt.gates.evaluate(now=h.now()).new_entries_allowed
    assert h.broker_orders(coid) == 1  # read-only handling: no order write of any kind
    assert h.position() == D(10)  # the ledger quantity is never "corrected" automatically


def test_mismatch_block_and_halt_survive_restart(h: Harness) -> None:
    holding(h)
    price(h, "5.00")
    broker_split(h, D(10))
    cycle_after(h)
    h.restart()
    codes = gate_codes(h)
    assert {"POSITION_QTY_MISMATCH", "PERSISTENT_HALT"} <= codes
    with h.db.read() as s:
        assert TradingStateRepository().get(s).reason_code == "RECON_POSITION"


# ============================================================ verification unavailable
def test_positions_read_failure_blocks_and_defers_without_a_new_halt(h: Harness) -> None:
    holding(h)
    price(h, "20.00")  # would be a weekly breach if quantities were verified
    h.sim.fail_next("get_positions", ServiceUnavailableError("503", status_code=503))
    report = cycle_after(h)
    assert report.positions is not None
    assert report.positions.status is VerificationStatus.UNAVAILABLE
    assert report.loss is not None and report.loss.loss_events_deferred is not None
    assert sum(loss_events(h).values()) == 0
    assert report.halts_raised == ()  # transient: existing outage policy, no invented halt
    assert report.failure_categories == ("SERVER_ERROR",)
    status = h.rt.gates.evaluate(now=h.now())
    assert GateReason.POSITION_QTY_UNVERIFIED.value in status.reason_codes
    assert not status.new_entries_allowed  # a failed verification never permits trading
    # the next cycle with a working read verifies again and records the genuine loss
    report = cycle_after(h)
    assert report.positions is not None and report.positions.verified
    assert loss_events(h)[LossEventKind.WEEKLY_HALT] == 1


# ============================================================ existing events are kept
def test_existing_lockout_and_weekly_halt_survive_a_later_mismatch(h: Harness) -> None:
    holding(h)
    price(h, "20.00")
    cycle_after(h)
    before = loss_events(h)
    assert before[LossEventKind.DAILY_LOCKOUT] == before[LossEventKind.WEEKLY_HALT] == 1
    price(h, "2.00")
    broker_split(h, D(10))
    report = cycle_after(h)
    assert loss_events(h) == before  # nothing erased, nothing new
    assert report.loss is not None
    assert {LossBlocker.DAILY_LOSS_LOCKOUT, LossBlocker.WEEKLY_LIMIT_ACTIVE,
            LossBlocker.POSITION_QTY_MISMATCH} <= set(report.loss.blockers)  # fmt: skip


# ============================================================ freshness and same state
def test_stale_or_foreign_verification_defers_events(h: Harness) -> None:
    holding(h)
    price(h, "20.00")
    h.advance(timedelta(minutes=1))
    now = h.now()
    ledger = {SYMBOL: D(10)}
    stale = PositionVerification(VerificationStatus.VERIFIED, now - timedelta(minutes=10),
                                 ledger, dict(ledger))  # fmt: skip
    other = PositionVerification(VerificationStatus.VERIFIED, now, {SYMBOL: D(7)},
                                 {SYMBOL: D(7)})  # fmt: skip
    record_marks(h.rt)
    for verification, why in ((stale, "stale"), (other, "changed"), (None, "no position")):
        report = h.rt.monitor.evaluate(now=now, verification=verification)
        assert report.loss_events_deferred is not None and why in report.loss_events_deferred
        assert sum(loss_events(h).values()) == 0
    fresh = PositionVerification(VerificationStatus.VERIFIED, now, ledger, dict(ledger))
    assert h.rt.monitor.evaluate(now=now, verification=fresh).loss_events_deferred is None
    assert loss_events(h)[LossEventKind.WEEKLY_HALT] == 1


def test_max_age_bounds_freshness_exactly(h: Harness) -> None:
    holding(h)
    now = h.now()
    ledger = {SYMBOL: D(10)}
    limit = h.rt.monitor._config.quantity_verification_max_age
    ok = PositionVerification(VerificationStatus.VERIFIED, now - limit, ledger, dict(ledger))
    old = replace(ok, verified_at=now - limit - timedelta(microseconds=1))
    assert ok.permits_loss_events(now=now, ledger_qty=ledger, max_age=limit) is None
    assert old.permits_loss_events(now=now, ledger_qty=ledger, max_age=limit) is not None


# ============================================================ comparison rules (pure)
def test_compare_positions_rules() -> None:
    at = datetime(2026, 3, 2, 15, tzinfo=UTC)

    def pos(sym: str, qty: str) -> BrokerPosition:
        return BrokerPosition(symbol=sym, qty=D(qty), avg_entry_price=D("1"))

    ledger = {"AAPL": D(10)}
    assert compare_positions(ledger, [pos("AAPL", "10")], lag_symbols=(), at=at).verified
    assert compare_positions({}, [], lag_symbols=(), at=at).verified
    cases = [
        ([pos("AAPL", "100")], (), VerificationStatus.MISMATCH),
        ([pos("AAPL", "100")], ("AAPL",), VerificationStatus.INCONCLUSIVE),  # fill lag
        ([], (), VerificationStatus.MISMATCH),  # broker flat, ledger holds
        ([pos("AAPL", "10"), pos("MSFT", "5")], (), VerificationStatus.MISMATCH),
        ([pos("AAPL", "10.5")], ("AAPL",), VerificationStatus.MISMATCH),  # fractional
        ([pos("AAPL", "-10")], (), VerificationStatus.MISMATCH),  # short
    ]
    for broker, lag, expected in cases:
        assert compare_positions(ledger, broker, lag_symbols=lag, at=at).status is expected
    unavailable = compare_positions(ledger, None, lag_symbols=(), at=at)
    assert unavailable.status is VerificationStatus.UNAVAILABLE
    restored = PositionVerification.from_json(unavailable.to_json())
    assert restored == unavailable


# ============================================================ Step 16.9A review
def book(h: Harness, side: Side, qty: str, px: str) -> None:
    """A fill adopted into the ledger after the verification (no broker involvement)."""
    at = h.now()
    order = make_order(h.db, side=side, qty=qty, limit=px, symbol=SYMBOL)
    if side is Side.BUY:
        reserve(h.rt.pf, h.db, order)
    h.rt.pf.process_fill(fill(order, qty, px, at=at), now=at)
    finish(h.rt.pf, h.db, order, OrderStatus.FILLED)


@pytest.mark.parametrize(("side", "qty", "after"), [(Side.BUY, "2", D(12)), (Side.SELL, "3", D(7))])
def test_ledger_change_after_verification_cannot_authorize_events(
    h: Harness, side: Side, qty: str, after: Decimal
) -> None:
    holding(h)
    price(h, "20.00")  # a weekly-sized loss on the verified 10 shares
    h.advance(timedelta(minutes=1))
    record_marks(h.rt)
    verification = verify_positions(h.rt)
    assert verification.verified and verification.ledger_qty == {SYMBOL: D(10)}
    book(h, side, qty, "20.00")  # newly adopted fill: the valued quantity is now different
    assert h.position() == after
    report = h.rt.monitor.evaluate(now=h.now(), verification=verification)
    assert report.loss_events_deferred is not None
    assert "changed after the position verification" in report.loss_events_deferred
    assert sum(loss_events(h).values()) == 0


def test_change_between_valuation_and_event_write_is_caught_in_the_write_transaction(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check sees the verified state; the ledger differs by the time events would
    be written. The re-check inside the write transaction refuses (atomic check+write)."""
    holding(h)
    price(h, "20.00")
    h.advance(timedelta(minutes=1))
    record_marks(h.rt)
    verification = verify_positions(h.rt)
    book(h, Side.BUY, "2", "20.00")  # the real ledger now holds 12
    real = ledger_quantities
    calls: list[int] = []

    def first_call_sees_verified_state(s: Any, pid: int) -> dict[str, Decimal]:
        calls.append(pid)
        return dict(verification.ledger_qty) if len(calls) == 1 else real(s, pid)

    monkeypatch.setattr(
        "swing_trading_bot.ledger.loss_limits.ledger_quantities", first_call_sees_verified_state
    )
    report = h.rt.monitor.evaluate(now=h.now(), verification=verification)
    assert len(calls) == 2  # valuation-time check, then the in-transaction re-check
    assert report.loss_events_deferred is not None
    assert "changed after the position verification" in report.loss_events_deferred
    assert sum(loss_events(h).values()) == 0


@pytest.mark.parametrize(("age", "deferred"), [(120, False), (121, True)])
def test_verification_older_than_120_seconds_is_stale(
    h: Harness,
    age: int,
    deferred: bool,
) -> None:
    assert h.rt.monitor._config.quantity_verification_max_age == timedelta(seconds=120)
    holding(h)
    price(h, "20.00")
    h.advance(timedelta(minutes=1))
    record_marks(h.rt)
    now = h.now()
    ledger = {SYMBOL: D(10)}
    v = PositionVerification(VerificationStatus.VERIFIED, now - timedelta(seconds=age),
                             ledger, dict(ledger))  # fmt: skip
    report = h.rt.monitor.evaluate(now=now, verification=v)
    assert (report.loss_events_deferred is not None) is deferred
    assert loss_events(h)[LossEventKind.WEEKLY_HALT] == (0 if deferred else 1)


def test_broker_reads_refuse_to_run_inside_a_transaction(h: Harness) -> None:
    holding(h)
    with pytest.raises(NestedTransactionError), h.db.transaction():
        verify_positions(h.rt)


def test_no_automatic_price_or_quantity_adjustment(h: Harness) -> None:
    holding(h)
    price(h, "5.00")
    broker_split(h, D(10))
    cycle_after(h)
    assert h.position() == D(10)  # ledger quantity untouched
    with h.db.read() as s:
        snap = h.rt.pf.snapshot(s, now=h.now())
    marks = {p.symbol: p.mark.price for p in snap.positions if p.mark is not None}
    assert marks == {SYMBOL: D("5.00")}  # the observed price is recorded as observed
