"""Step 5: calendar persistence, restart behaviour, conflict rejection and migration 0002."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from swing_trading_bot.domain.calendar import (
    CalendarCoverageError,
    CalendarFetch,
    CalendarKind,
    SettlementDateUnavailableError,
    StaleCalendarError,
)
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.calendar_store import CalendarStore, FetchStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import ConstraintViolationError, MigrationError
from swing_trading_bot.ledger.migrator import packaged_migrations
from tests.support.calendar_fixture import (
    FIXTURE_END,
    fixture_sessions,
    settlement_fetch,
    trading_fetch,
)

AS_OF = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
FETCHED = AS_OF - timedelta(hours=1)
MAX_AGE = timedelta(hours=36)
store = CalendarStore()
D = date


def _record(db: Database, fetch: CalendarFetch) -> FetchStatus:
    with db.transaction() as tx:
        return store.record_fetch(tx, fetch).status


def _seed(db: Database, fetched: datetime = FETCHED) -> None:
    assert _record(db, trading_fetch(fetched)) is FetchStatus.ACCEPTED
    assert _record(db, settlement_fetch(fetched)) is FetchStatus.ACCEPTED


def test_empty_database_has_no_calendar(db: Database) -> None:
    with db.read() as s:
        calendar = store.calendar(s, as_of=AS_OF, max_age=MAX_AGE)
        with pytest.raises(CalendarCoverageError):
            calendar.is_trading_session(D(2026, 11, 9))
        assert not calendar.calendar_coverage(D(2026, 11, 9), D(2026, 11, 9)).complete


def test_persisted_calendar_answers_queries(db: Database) -> None:
    _seed(db)
    with db.read() as s:
        calendar = store.calendar(s, as_of=AS_OF, max_age=MAX_AGE)
        assert calendar.settlement_date(D(2026, 11, 10)) == D(2026, 11, 12)
        assert calendar.next_trading_session(D(2026, 11, 25)) == D(2026, 11, 27)
        assert calendar.session(D(2026, 11, 27)).is_early_close
        assert s.scalar("SELECT count(*) FROM market_calendar") == len(fixture_sessions())


def test_cache_survives_restart(db: Database, db_path: Path) -> None:
    _seed(db)
    db.close()
    with Database.open(db_path, RuntimeMode.SIMULATE) as again, again.read() as s:
        calendar = store.calendar(s, as_of=AS_OF, max_age=MAX_AGE)
        assert calendar.settlement_date(D(2026, 12, 31)) == D(2027, 1, 4)
        assert calendar.horizon_status(warning_days=365).trading_horizon == FIXTURE_END
        log = store.fetch_log(s)
    assert [(e.kind, e.status) for e in log] == [
        (CalendarKind.TRADING, FetchStatus.ACCEPTED),
        (CalendarKind.SETTLEMENT, FetchStatus.ACCEPTED),
    ]
    assert log[0].row_count == len(fixture_sessions())
    assert len(log[0].payload_sha256) == 64


def test_staleness_after_restart_and_refresh(db: Database) -> None:
    _seed(db)
    later = FETCHED + timedelta(days=3)
    with db.read() as s:
        calendar = store.calendar(s, as_of=later, max_age=MAX_AGE)
        with pytest.raises(StaleCalendarError):
            calendar.settlement_date(D(2026, 11, 20))
    _seed(db, fetched=later)  # identical content, newer fetch: confirms rows
    with db.read() as s:
        calendar = store.calendar(s, as_of=later, max_age=MAX_AGE)
        assert calendar.settlement_date(D(2026, 11, 20)) == D(2026, 11, 23)
        confirmed = s.scalar(
            "SELECT last_confirmed_fetch_id FROM market_calendar WHERE session_date = '2026-11-20'"
        )
        first = s.scalar(
            "SELECT first_fetch_id FROM market_calendar WHERE session_date = '2026-11-20'"
        )
    assert (first, confirmed) == (1, 3)


def test_conflicting_refetch_rejected_and_audited(db: Database) -> None:
    _seed(db)
    sessions = [s for s in fixture_sessions() if s.session_date != D(2026, 11, 16)]
    conflicting = replace(trading_fetch(FETCHED + timedelta(hours=2)), sessions=tuple(sessions))
    with db.transaction() as tx:
        result = store.record_fetch(tx, conflicting)
    assert result.status is FetchStatus.REJECTED
    assert any("2026-11-16" in p and "disappeared" in p for p in result.problems)
    with db.read() as s:
        calendar = store.calendar(s, as_of=AS_OF, max_age=MAX_AGE)
        assert calendar.is_trading_session(D(2026, 11, 16))  # stored history unchanged
        log = store.fetch_log(s)
    assert log[-1].status is FetchStatus.REJECTED
    assert log[-1].problems == result.problems


def test_rejected_fetch_does_not_extend_coverage(db: Database) -> None:
    bad = CalendarFetch(
        CalendarKind.TRADING, D(2026, 11, 1), D(2026, 11, 30), FETCHED, "test",
        sessions=tuple(
            replace(s, settlement_date=D(2026, 11, 14)) if s.session_date == D(2026, 11, 13) else s
            for s in fixture_sessions(D(2026, 11, 1), D(2026, 11, 30))
        ),
    )  # fmt: skip
    with db.transaction() as tx:
        result = store.record_fetch(tx, bad)
    assert not result.accepted
    with db.read() as s:
        assert s.scalar("SELECT count(*) FROM market_calendar") == 0
        calendar = store.calendar(s, as_of=AS_OF, max_age=MAX_AGE)
        with pytest.raises(CalendarCoverageError):
            calendar.is_trading_session(D(2026, 11, 9))


def test_cross_calendar_contradiction_rejected(db: Database) -> None:
    assert _record(db, trading_fetch(FETCHED)) is FetchStatus.ACCEPTED
    days = tuple(d for d in settlement_fetch(FETCHED).settlement_days if d != D(2026, 11, 10))
    with db.transaction() as tx:
        result = store.record_fetch(tx, replace(settlement_fetch(FETCHED), settlement_days=days))
    assert not result.accepted
    assert any("2026-11-09" in p for p in result.problems)
    with db.read() as s:
        assert s.scalar("SELECT count(*) FROM settlement_days") == 0


def test_settlement_dates_filled_in_by_later_fetch(db: Database) -> None:
    assert _record(db, trading_fetch(FETCHED, with_settlement_dates=False)) is FetchStatus.ACCEPTED
    with db.read() as s, pytest.raises(SettlementDateUnavailableError):
        store.calendar(s, as_of=AS_OF, max_age=MAX_AGE).settlement_date(D(2026, 11, 9))
    assert _record(db, trading_fetch(FETCHED)) is FetchStatus.ACCEPTED
    with db.read() as s:
        assert store.calendar(s, as_of=AS_OF, max_age=MAX_AGE).settlement_date(D(2026, 11, 9)) == D(
            2026, 11, 10
        )


def test_rejection_of_internal_problems_is_recorded(db: Database) -> None:
    dup = fixture_sessions(D(2026, 11, 9), D(2026, 11, 9))
    fetch = CalendarFetch(
        CalendarKind.TRADING, D(2026, 11, 9), D(2026, 11, 9), FETCHED, "test",
        sessions=(*dup, *dup),
    )  # fmt: skip
    with db.transaction() as tx:
        result = store.record_fetch(tx, fetch)
    assert result.problems == ("2026-11-09: duplicate session",)


# --------------------------------------------------------------- migration 0002


def test_migration_0002_schema(db: Database) -> None:
    assert db.pragma("user_version") == len(packaged_migrations()) == 2
    with db.read() as s:
        tables = {
            str(r[0]) for r in s.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        cols = [str(r[1]) for r in s.fetch_all("PRAGMA table_info(market_calendar)")]
        strict = s.scalar("SELECT strict FROM pragma_table_list WHERE name = 'market_calendar'")
    assert {"calendar_fetches", "settlement_days", "market_calendar"} <= tables
    assert "market_calendar_v2" not in tables
    assert cols == [
        "session_date", "open_at", "close_at", "settlement_date",
        "first_fetch_id", "last_confirmed_fetch_id",
    ]  # fmt: skip
    assert strict == 1


def test_calendar_tables_are_protected(db: Database) -> None:
    _seed(db)

    def write(sql: str) -> None:
        with db.transaction() as tx:
            tx.execute(sql)

    with pytest.raises(ConstraintViolationError, match="immutable"):
        write("UPDATE market_calendar SET close_at = open_at WHERE session_date = '2026-11-09'")
    with pytest.raises(ConstraintViolationError, match="cannot change once set"):
        write("UPDATE market_calendar SET settlement_date = '2026-11-11' "
              "WHERE session_date = '2026-11-09'")  # fmt: skip
    with pytest.raises(ConstraintViolationError, match="cannot be deleted"):
        write("DELETE FROM market_calendar")
    with pytest.raises(ConstraintViolationError, match="cannot be deleted"):
        write("DELETE FROM settlement_days")
    with pytest.raises(ConstraintViolationError, match="append-only"):
        write("UPDATE calendar_fetches SET status = 'REJECTED'")
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        write(
            "INSERT INTO calendar_fetches (kind, range_start, range_end, fetched_at, source, "
            "row_count, payload_sha256, status, problems_json) VALUES ('TRADING', '2026-11-09', "
            "'2026-11-09', '2026-11-09T15:00:00.000000+00:00', 't', 0, "
            f"'{'0' * 64}', 'ACCEPTED', '[\"problem\"]')"
        )
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        write("INSERT INTO settlement_days VALUES ('2030-01-02', 999, 999)")


def test_upgrade_from_schema_version_1(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    Database.create(path, RuntimeMode.SIMULATE, migrations=packaged_migrations()[:1]).close()
    with Database.open(path, RuntimeMode.SIMULATE) as upgraded:
        assert upgraded.pragma("user_version") == 2
        with upgraded.transaction() as tx:
            assert store.record_fetch(tx, trading_fetch(FETCHED)).accepted


def test_migration_0002_refuses_legacy_rows_without_provenance(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    Database.create(path, RuntimeMode.SIMULATE, migrations=packaged_migrations()[:1]).close()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "INSERT INTO market_calendar VALUES ('2026-11-09', '2026-11-09T14:30:00.000000+00:00', "
        "'2026-11-09T21:00:00.000000+00:00', '2026-11-10', '2026-11-09T12:00:00.000000+00:00')"
    )
    conn.close()
    with pytest.raises(MigrationError, match="NOT NULL"):
        Database.open(path, RuntimeMode.SIMULATE)
    with Database.open(path, RuntimeMode.SIMULATE, migrations=packaged_migrations()[:1]) as old:
        assert old.pragma("user_version") == 1  # rolled back, old table intact
        with old.read() as s:
            assert s.scalar("SELECT count(*) FROM market_calendar") == 1
