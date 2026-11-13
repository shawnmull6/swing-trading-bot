"""Step 15B.5A: the corrected SETTLEMENT calendar through the real adapter, the calendar
store, T+1 cash settlement, init-ledger and the C4 contract check — offline.

Every fetch here goes through ``AlpacaPaperReadAdapter.get_calendar`` against the fake
account, whose SETTLEMENT response has the observed real shape (trade-date rows selected
by ``settlement_date``). Values are synthetic fixture data.

The early-release question is answered by construction and by test: a settlement date
comes from the session's own ``settlement_date`` and/or the first settlement day after the
trade; when both exist they must agree, and when neither can be established proceeds stay
unsettled (``UNRESOLVED_SETTLEMENT``).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as Dec
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.domain.calendar import CalendarFetch, CalendarKind
from swing_trading_bot.domain.enums import Side
from swing_trading_bot.ledger.calendar_store import CalendarStore, FetchStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.portfolio import EntryBlocker, FillOutcome, VirtualPortfolio
from tests.contract import checks as contract_checks
from tests.contract.test_contract_replay import synthetic_account, wire
from tests.support.alpaca_account import Clock, FakeAlpacaAccount, _calendar_row
from tests.support.calendar_fixture import fixture_sessions, fixture_settlement_days
from tests.support.cli_env import CliEnv, cli_env
from tests.support.portfolio import CONFIG, fill, make_order, new_ledger, reserve

D = date
NOV_START, NOV_END = D(2026, 11, 2), D(2026, 12, 31)
store = CalendarStore()


def fetch(kind: CalendarKind, start: date, end: date, *, at: datetime,
          rows: list[dict[str, Any]] | None = None) -> CalendarFetch:  # fmt: skip
    """A fetch through the real adapter; ``rows`` overrides the fake's response."""
    acct = FakeAlpacaAccount(clock=Clock(at))
    if rows is not None:
        acct.transport.respond("trading", "/calendar", json.dumps(rows))
    return acct.adapter().get_calendar(kind, start, end)


def settlement_rows(start: date, end: date, **swap: date) -> list[dict[str, Any]]:
    """Real-shaped SETTLEMENT rows; ``swap`` maps an ISO trade date to a new settlement."""
    out, used = [], set()
    for s in fixture_sessions(start - timedelta(days=10), end):
        if s.settlement_date is not None and start <= s.settlement_date <= end:
            r = _calendar_row(s)
            key = f"d{s.session_date:%Y%m%d}"
            if key in swap:
                r["settlement_date"] = swap[key].isoformat()
                used.add(key)
            out.append(r)
    assert used == set(swap), f"swap keys matched no row: {sorted(set(swap) - used)}"
    return out


def record(db: Database, f: CalendarFetch) -> FetchStatus:
    with db.transaction() as tx:
        return store.record_fetch(tx, f).status


def ledger(tmp_path: Path, start: date, end: date, *, at: datetime,
           trading_rows: list[dict[str, Any]] | None = None
           ) -> tuple[Database, VirtualPortfolio]:  # fmt: skip
    db, pf = new_ledger(tmp_path / "s.sqlite3", with_calendar=False)
    assert record(db, fetch(CalendarKind.TRADING, start, end, at=at,
                            rows=trading_rows)) is FetchStatus.ACCEPTED  # fmt: skip
    assert record(db, fetch(CalendarKind.SETTLEMENT, start, end, at=at)) is FetchStatus.ACCEPTED
    return db, pf


def settlement_of(db: Database, trade: date, *, at: datetime) -> date:
    with db.read() as s:
        return store.calendar(s, as_of=at, max_age=CONFIG.calendar_max_age).settlement_date(trade)


def settled_cash(db: Database, pf: VirtualPortfolio, at: datetime) -> Any:
    with db.read() as s:
        return pf.snapshot(s, now=at).balances.settled_cash


def buy_then_sell(db: Database, pf: VirtualPortfolio, buy_at: datetime, sell_at: datetime
                  ) -> Any:  # fmt: skip
    order = make_order(db, qty="10", limit="100.00")
    reserve(pf, db, order)
    assert pf.process_fill(fill(order, "10", "100.00", at=buy_at), now=buy_at).outcome \
        is FillOutcome.APPLIED  # fmt: skip
    sell = make_order(db, side=Side.SELL, qty="10", limit="120.00")
    return pf.process_fill(fill(sell, "10", "120.00", at=sell_at), now=sell_at)


# ============================================================ derived settlement days
@pytest.mark.parametrize(("start", "end"), [
    (NOV_START, NOV_END),
    (D(2026, 11, 12), D(2026, 11, 30)),   # starts on the shared Veterans Day settlement
    (D(2026, 12, 1), D(2027, 1, 29)),     # Christmas and New Year
])  # fmt: skip
def test_settlement_days_match_eligible_days_and_skip_holidays(start: date, end: date) -> None:
    f = fetch(CalendarKind.SETTLEMENT, start, end, at=datetime(2026, 11, 13, tzinfo=UTC))
    assert f.problems() == []
    assert list(f.settlement_days) == fixture_settlement_days(start, end)
    for holiday in (D(2026, 11, 11), D(2026, 11, 26), D(2026, 12, 25), D(2027, 1, 1)):
        assert holiday not in f.settlement_days


def test_real_shape_has_out_of_range_trade_dates_and_shared_settlements() -> None:
    rows = settlement_rows(NOV_START, NOV_END)
    assert rows[0]["date"] < NOV_START.isoformat() <= rows[0]["settlement_date"]
    shared = [r["date"] for r in rows if r["settlement_date"] == "2026-11-12"]
    assert shared == ["2026-11-10", "2026-11-11"]


# ============================================================ holidays and T+1
@pytest.mark.parametrize(("trade", "settles"), [
    (D(2026, 11, 10), D(2026, 11, 12)),   # over Veterans Day
    (D(2026, 11, 11), D(2026, 11, 12)),   # traded on Veterans Day
    (D(2026, 11, 25), D(2026, 11, 27)),   # over Thanksgiving
    (D(2026, 11, 27), D(2026, 11, 30)),   # early close, over the weekend
    (D(2026, 12, 24), D(2026, 12, 28)),   # over Christmas
    (D(2026, 12, 31), D(2027, 1, 4)),     # over New Year, into next year
])  # fmt: skip
def test_settlement_dates_through_holidays(tmp_path: Path, trade: date, settles: date) -> None:
    at = datetime(2026, 11, 13, tzinfo=UTC)
    db, _ = ledger(tmp_path, NOV_START, D(2027, 1, 29), at=at)
    assert settlement_of(db, trade, at=at) == settles
    db.close()


def test_t_plus_1_cash_over_veterans_day(tmp_path: Path) -> None:
    tue = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    db, pf = ledger(tmp_path, NOV_START, NOV_END, at=tue - timedelta(minutes=5))
    result = buy_then_sell(db, pf, tue - timedelta(hours=1), tue)
    assert result.settles_on == D(2026, 11, 12)
    for when, settled in [
        (tue, Dec("4000.00")),
        (datetime(2026, 11, 11, 20, 0, tzinfo=UTC), Dec("4000.00")),  # Veterans Day
        (datetime(2026, 11, 12, 4, 59, tzinfo=UTC), Dec("4000.00")),  # 23:59 ET Nov 11
        (datetime(2026, 11, 12, 5, 0, tzinfo=UTC), Dec("5200.00")),  # 00:00 ET Nov 12
    ]:
        assert settled_cash(db, pf, when) == settled, when
    db.close()


# ============================================================ year end without January data
DEC_START, DEC_END = D(2026, 12, 1), D(2026, 12, 31)
FETCHED = datetime(2026, 12, 30, 13, 0, tzinfo=UTC)
BUY_AT = datetime(2026, 12, 30, 15, 0, tzinfo=UTC)
SELL_AT = datetime(2026, 12, 31, 15, 0, tzinfo=UTC)
NOT_YET = [
    datetime(2026, 12, 31, 22, 0, tzinfo=UTC),
    datetime(2027, 1, 1, 17, 0, tzinfo=UTC),
    datetime(2027, 1, 2, 17, 0, tzinfo=UTC),
    datetime(2027, 1, 4, 4, 59, tzinfo=UTC),
]


def test_dec_31_trade_never_settles_before_its_january_date(tmp_path: Path) -> None:
    """January settlement coverage is unavailable: the session's own settlement_date
    (2027-01-04) is used, and proceeds stay unsettled through 23:59 ET on Jan 3."""
    db, pf = ledger(tmp_path, DEC_START, DEC_END, at=FETCHED)
    with db.read() as s:
        cal = store.calendar(s, as_of=SELL_AT, max_age=CONFIG.calendar_max_age)
        assert not cal.calendar_coverage(D(2027, 1, 1), D(2027, 1, 4)).complete
    result = buy_then_sell(db, pf, BUY_AT, SELL_AT)
    assert result.settles_on == D(2027, 1, 4)
    for when in NOT_YET:
        assert settled_cash(db, pf, when) == Dec("4000.00"), when
    assert settled_cash(db, pf, datetime(2027, 1, 4, 5, 0, tzinfo=UTC)) == Dec("5200.00")
    db.close()


def test_dec_31_trade_without_any_settlement_source_stays_unsettled(tmp_path: Path) -> None:
    rows = [_calendar_row(s) for s in fixture_sessions(DEC_START, DEC_END)]
    rows[-1]["settlement_date"] = None  # the provider omits Dec 31's settlement date
    db, pf = ledger(tmp_path, DEC_START, DEC_END, at=FETCHED, trading_rows=rows)
    result = buy_then_sell(db, pf, BUY_AT, SELL_AT)
    assert result.settles_on is None  # never guessed past the covered range
    later = datetime(2027, 1, 8, 17, 0, tzinfo=UTC)
    assert pf.resolve_settlements(now=later) == 0
    with db.read() as s:
        snap = pf.snapshot(s, now=later)
    assert snap.balances.settled_cash == Dec("4000.00")
    assert snap.balances.unsettled_proceeds == Dec("1200.00")
    assert EntryBlocker.UNRESOLVED_SETTLEMENT in snap.entry_blockers
    db.close()


# ============================================================ store acceptance / rejection
AT = datetime(2026, 11, 13, tzinfo=UTC)


def test_out_of_range_and_weekend_settlements_are_rejected(tmp_path: Path) -> None:
    db, _ = new_ledger(tmp_path / "r.sqlite3", with_calendar=False)
    beyond = settlement_rows(NOV_START, NOV_END, d20261230=D(2027, 1, 4))
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT,
                            rows=beyond)) is FetchStatus.REJECTED  # fmt: skip
    weekend = settlement_rows(NOV_START, NOV_END, d20261106=D(2026, 11, 7))
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT,
                            rows=weekend)) is FetchStatus.REJECTED  # fmt: skip
    with db.read() as s:
        assert store.load(s).settlement_days == frozenset()
    db.close()


def test_conflicting_repeat_fetch_is_rejected(tmp_path: Path) -> None:
    db, _ = new_ledger(tmp_path / "c.sqlite3", with_calendar=False)
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT)) \
        is FetchStatus.ACCEPTED  # fmt: skip
    rows = [r for r in settlement_rows(NOV_START, NOV_END) if r["settlement_date"] != "2026-11-12"]
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT,
                            rows=rows)) is FetchStatus.REJECTED  # fmt: skip
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT)) \
        is FetchStatus.ACCEPTED  # an identical re-fetch is fine  # fmt: skip
    db.close()


def test_provider_disagreement_is_rejected_and_never_settles_early(tmp_path: Path) -> None:
    """SETTLEMENT says Nov 10 settles on Veterans Day; the session says Nov 12."""
    db, _ = new_ledger(tmp_path / "p.sqlite3", with_calendar=False)
    assert record(db, fetch(CalendarKind.TRADING, NOV_START, NOV_END, at=AT)) \
        is FetchStatus.ACCEPTED  # fmt: skip
    early = settlement_rows(NOV_START, NOV_END, d20261110=D(2026, 11, 11))
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT,
                            rows=early)) is FetchStatus.REJECTED  # fmt: skip
    assert settlement_of(db, D(2026, 11, 10), at=AT) == D(2026, 11, 12)  # never Nov 11
    db.close()


def test_session_claiming_a_non_settlement_day_is_rejected(tmp_path: Path) -> None:
    db, _ = new_ledger(tmp_path / "q.sqlite3", with_calendar=False)
    assert record(db, fetch(CalendarKind.SETTLEMENT, NOV_START, NOV_END, at=AT)) \
        is FetchStatus.ACCEPTED  # fmt: skip
    rows = [_calendar_row(s) for s in fixture_sessions(NOV_START, NOV_END)]
    for r in rows:
        if r["date"] == "2026-11-10":
            r["settlement_date"] = "2026-11-11"
    assert record(db, fetch(CalendarKind.TRADING, NOV_START, NOV_END, at=AT,
                            rows=rows)) is FetchStatus.REJECTED  # fmt: skip
    db.close()


# ============================================================ init-ledger and C4
@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def test_init_ledger_accepts_the_real_shaped_settlement_calendar(e: CliEnv) -> None:
    e.init()
    with e.open() as db, db.read() as s:
        rows = s.fetch_all("SELECT kind, source, status FROM calendar_fetches ORDER BY id")
        days = {r[0] for r in s.fetch_all("SELECT settlement_date FROM settlement_days")}
    assert ("SETTLEMENT", "alpaca", "ACCEPTED") in [tuple(r) for r in rows]
    assert all(r[2] == "ACCEPTED" for r in rows)
    assert "2026-11-12" in days and "2026-11-11" not in days


def test_contract_c4_passes_on_the_real_shape() -> None:
    ctx, _spy = wire(synthetic_account())
    obs = contract_checks.check_calendars(ctx)
    assert obs["internally_consistent"] is True
    assert obs["settlement_days_differing_from_fixture"] == []
    assert obs["session_settlement_dates_are_settlement_days"] is True
