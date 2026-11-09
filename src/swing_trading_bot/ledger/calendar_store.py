"""Persistence for the trading/settlement calendar (migration 0002, M1 step 5).

``record_fetch`` validates a provider fetch on three levels before anything is
written:

1. the fetch is internally consistent (:meth:`CalendarFetch.problems`);
2. it does not change already-accepted calendar history in overlapping dates
   (:meth:`CalendarData.conflicts_with`);
3. merged with stored data, the two calendars do not contradict each other
   (:meth:`CalendarData.audit`).

An accepted fetch upserts its rows (confirming identical rows, filling in a missing
per-session settlement date) and becomes coverage. A rejected fetch writes **only** an
audit row with its problems — coverage and rows are untouched — and the result is
returned, not raised, so the audit row commits with the caller's transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from swing_trading_bot.domain.calendar import (
    CalendarData,
    CalendarFetch,
    CalendarKind,
    CoverageRange,
    TradingCalendar,
    TradingSession,
)
from swing_trading_bot.ledger.codec import (
    date_from_db,
    date_to_db,
    int_from_db,
    opt_date_from_db,
    opt_date_to_db,
    str_from_db,
    ts_from_db,
    ts_to_db,
)
from swing_trading_bot.ledger.db import Session, Tx


class FetchStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class FetchResult:
    fetch_id: int
    status: FetchStatus
    problems: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.status is FetchStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class FetchLogEntry:
    id: int
    kind: CalendarKind
    start: date
    end: date
    fetched_at: datetime
    source: str
    row_count: int
    payload_sha256: str
    status: FetchStatus
    problems: tuple[str, ...]


class CalendarStore:
    def load(self, s: Session) -> CalendarData:
        sessions = {
            date_from_db(r[0]): TradingSession(
                session_date=date_from_db(r[0]),
                open_at=ts_from_db(r[1]),
                close_at=ts_from_db(r[2]),
                settlement_date=opt_date_from_db(r[3]),
            )
            for r in s.fetch_all(
                "SELECT session_date, open_at, close_at, settlement_date FROM market_calendar"
            )
        }
        days = frozenset(
            date_from_db(r[0]) for r in s.fetch_all("SELECT settlement_date FROM settlement_days")
        )
        coverage = tuple(
            CoverageRange(
                kind=CalendarKind(str_from_db(r[0])),
                start=date_from_db(r[1]),
                end=date_from_db(r[2]),
                fetched_at=ts_from_db(r[3]),
                fetch_id=int_from_db(r[4]),
            )
            for r in s.fetch_all(
                "SELECT kind, range_start, range_end, fetched_at, id FROM calendar_fetches "
                "WHERE status = 'ACCEPTED' ORDER BY id"
            )
        )
        return CalendarData(sessions=sessions, settlement_days=days, coverage=coverage)

    def calendar(self, s: Session, *, as_of: datetime, max_age: timedelta) -> TradingCalendar:
        return TradingCalendar(self.load(s), as_of=as_of, max_age=max_age)

    def record_fetch(self, tx: Tx, fetch: CalendarFetch) -> FetchResult:
        current = self.load(tx)
        problems = list(fetch.problems())
        if not problems:
            problems.extend(current.conflicts_with(fetch))
        if not problems:
            existing = set(current.audit())
            problems.extend(p for p in current.with_fetch(fetch).audit() if p not in existing)
        status = FetchStatus.REJECTED if problems else FetchStatus.ACCEPTED
        fetch_id = tx.insert(
            "INSERT INTO calendar_fetches (kind, range_start, range_end, fetched_at, source, "
            "row_count, payload_sha256, status, problems_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fetch.kind.value,
                date_to_db(fetch.start),
                date_to_db(fetch.end),
                ts_to_db(fetch.fetched_at),
                fetch.source,
                fetch.row_count,
                fetch.payload_sha256,
                status.value,
                json.dumps(problems),
            ),
        )
        if status is FetchStatus.ACCEPTED:
            self._apply(tx, fetch, fetch_id)
        return FetchResult(fetch_id, status, tuple(problems))

    @staticmethod
    def _apply(tx: Tx, fetch: CalendarFetch, fetch_id: int) -> None:
        for s in fetch.sessions:
            tx.execute(
                "INSERT INTO market_calendar (session_date, open_at, close_at, settlement_date, "
                "first_fetch_id, last_confirmed_fetch_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_date) DO UPDATE SET "
                "settlement_date = COALESCE(market_calendar.settlement_date, "
                "excluded.settlement_date), "
                "last_confirmed_fetch_id = excluded.last_confirmed_fetch_id",
                (
                    date_to_db(s.session_date),
                    ts_to_db(s.open_at),
                    ts_to_db(s.close_at),
                    opt_date_to_db(s.settlement_date),
                    fetch_id,
                    fetch_id,
                ),
            )
        for d in fetch.settlement_days:
            tx.execute(
                "INSERT INTO settlement_days (settlement_date, first_fetch_id, "
                "last_confirmed_fetch_id) VALUES (?, ?, ?) ON CONFLICT(settlement_date) "
                "DO UPDATE SET last_confirmed_fetch_id = excluded.last_confirmed_fetch_id",
                (date_to_db(d), fetch_id, fetch_id),
            )

    def fetch_log(self, s: Session) -> list[FetchLogEntry]:
        rows = s.fetch_all(
            "SELECT id, kind, range_start, range_end, fetched_at, source, row_count, "
            "payload_sha256, status, problems_json FROM calendar_fetches ORDER BY id"
        )
        return [
            FetchLogEntry(
                id=int_from_db(r[0]),
                kind=CalendarKind(str_from_db(r[1])),
                start=date_from_db(r[2]),
                end=date_from_db(r[3]),
                fetched_at=ts_from_db(r[4]),
                source=str_from_db(r[5]),
                row_count=int_from_db(r[6]),
                payload_sha256=str_from_db(r[7]),
                status=FetchStatus(str_from_db(r[8])),
                problems=tuple(json.loads(str_from_db(r[9]))),
            )
            for r in rows
        ]
