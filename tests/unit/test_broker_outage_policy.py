"""Step 14.1: the outage streak transition and its exact boundaries (pure + persisted)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerRequestError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UnknownBrokerStatusError,
)
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.broker_outage import (
    OutageEvent,
    OutagePolicy,
    OutageState,
    load_streak,
    next_failure_state,
    record_complete_cycle,
    record_failed_cycle,
)
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.oms.lifecycle import read_error_outage_category

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
P = OutagePolicy()
S = timedelta(seconds=1)
US = timedelta(microseconds=1)


def feed(*offsets: timedelta) -> tuple[list[OutageEvent], OutageState]:
    state: OutageState | None = None
    events = []
    for off in offsets:
        event, state = next_failure_state(state, cycle_at=T0 + off, category="X", policy=P)
        events.append(event)
    assert state is not None
    return events, state


def test_single_failure_never_escalates_however_long_ago() -> None:
    _, state = feed(timedelta(0))
    assert state.counted_cycles == 1 and not state.escalation_due(P)


def test_three_counted_cycles_need_thirty_minutes_inclusive() -> None:
    _, short = feed(timedelta(0), timedelta(minutes=15), timedelta(minutes=30) - US)
    assert short.counted_cycles == 3 and not short.escalation_due(P)
    _, exact = feed(timedelta(0), timedelta(minutes=15), timedelta(minutes=30))
    assert exact.escalation_due(P)


def test_two_cycles_never_escalate_even_after_hours() -> None:
    _, state = feed(timedelta(0), timedelta(hours=5))
    assert state.counted_cycles == 2 and not state.escalation_due(P)


def test_spacing_boundary_is_sixty_seconds_from_last_counted() -> None:
    events, state = feed(timedelta(0), 30 * S, 60 * S - US, 60 * S)
    assert events == [OutageEvent.STARTED, OutageEvent.TOO_SOON, OutageEvent.TOO_SOON,
                      OutageEvent.COUNTED]  # fmt: skip
    assert state.counted_cycles == 2 and state.last_failed_at == T0 + 60 * S


def test_rapid_retries_over_thirty_minutes_do_not_count() -> None:
    # 59 s apart for 35 minutes: only every second report is >= 60 s after a counted one.
    offsets = [59 * S * i for i in range(36)]
    _, state = feed(*offsets)
    assert state.counted_cycles == 18
    _, sparse = feed(*(59 * S * i for i in range(2)))  # two reports, 59 s apart
    assert sparse.counted_cycles == 1


def test_duplicate_or_older_reports_change_nothing() -> None:
    _, state = feed(timedelta(0), timedelta(minutes=5))
    for at in (T0 + timedelta(minutes=5), T0 + timedelta(minutes=1)):
        event, again = next_failure_state(state, cycle_at=at, category="Y", policy=P)
        assert event is OutageEvent.DUPLICATE and again == state


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        OutagePolicy(min_counted_cycles=1)
    with pytest.raises(ValueError, match="shorter"):
        OutagePolicy(min_duration=S, min_cycle_spacing=60 * S)


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (ServiceUnavailableError("503", status_code=503), "SERVER_ERROR"),
        (ServiceUnavailableError("t", kind="timeout"), "TIMEOUT"),
        (ServiceUnavailableError("c", kind="network"), "NETWORK"),
        (ServiceUnavailableError("sim"), "UNAVAILABLE"),
        (RateLimitedError("429"), "RATE_LIMITED"),
        (AuthenticationError("401", status_code=401), None),
        (InvalidBrokerDataError("float"), None),
        (UnknownBrokerStatusError("weird"), None),
        (OrderNotFoundError("404", status_code=404), None),
        (BrokerRequestError("400", status_code=400), None),
        (IncompleteRetrievalError("no progress"), None),
    ],
)
def test_failure_classification(exc: Exception, category: str | None) -> None:
    assert read_error_outage_category(exc) == category


def test_incomplete_retrieval_takes_its_cause() -> None:
    exc = IncompleteRetrievalError("window interrupted")
    exc.__cause__ = ServiceUnavailableError("x", kind="timeout")
    assert read_error_outage_category(exc) == "TIMEOUT"


# ============================================================ persistence
@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.create(tmp_path / "o.sqlite3", RuntimeMode.SIMULATE, now=T0)
    yield database
    database.close()


def failed(db: Database, off: timedelta, category: str = "SERVER_ERROR") -> OutageEvent:
    with db.transaction() as tx:
        return record_failed_cycle(tx, cycle_at=T0 + off, category=category, policy=P).event


def halt_events(db: Database) -> list[str]:
    with db.read() as s:
        return [e.reason_code for e in TradingStateRepository().events(s) if e.action == "HALT"]


def test_escalation_writes_one_halt_and_records_it(db: Database) -> None:
    assert failed(db, timedelta(0)) is OutageEvent.STARTED
    assert failed(db, timedelta(minutes=15)) is OutageEvent.COUNTED
    assert failed(db, timedelta(minutes=30), "RATE_LIMITED") is OutageEvent.ESCALATED
    for minute in (31, 33, 40):
        assert failed(db, timedelta(minutes=minute)) is OutageEvent.COUNTED  # no re-halt
    assert halt_events(db).count("BROKER_OUTAGE") == 1
    with db.read() as s:
        streak = load_streak(s)
        state = TradingStateRepository().get(s)
    assert streak is not None and streak.escalated_at == T0 + timedelta(minutes=30)
    assert streak.counted_cycles == 6 and streak.categories == ("SERVER_ERROR", "RATE_LIMITED")
    assert state.reason_code == "BROKER_OUTAGE" and "previously halted: INITIALIZED" in str(
        state.reason_detail
    )


def test_reset_keeps_the_halt_and_starts_fresh(db: Database) -> None:
    for minute in (0, 15, 30):
        failed(db, timedelta(minutes=minute))
    with db.transaction() as tx:
        stale = record_complete_cycle(tx, cycle_at=T0 + timedelta(minutes=20))
        assert stale.event is OutageEvent.DUPLICATE  # older than the streak: ignored
        assert record_complete_cycle(tx, cycle_at=T0 + timedelta(minutes=45)).event is (
            OutageEvent.RESET
        )
        assert load_streak(tx) is None
        assert record_complete_cycle(tx, cycle_at=T0 + timedelta(minutes=46)).event is (
            OutageEvent.NO_STREAK
        )
        assert TradingStateRepository().get(tx).reason_code == "BROKER_OUTAGE"
    assert failed(db, timedelta(minutes=50)) is OutageEvent.STARTED
