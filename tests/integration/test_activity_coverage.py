"""Step 9 refinement 2: activity windows establish coverage; cursors never do."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.activity_feed import collect_window, merge_activities
from swing_trading_bot.broker.errors import IncompleteRetrievalError, ServiceUnavailableError
from swing_trading_bot.broker.models import ActivityPage, BrokerActivity, SubmitOrderRequest
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from swing_trading_bot.domain.enums import OrderType, RuntimeMode, Side, TimeInForce
from swing_trading_bot.domain.ids import new_client_order_id
from swing_trading_bot.ledger.activity_coverage import advance_coverage, complete_through
from swing_trading_bot.ledger.db import Database
from tests.support.loss_limits import et

D = Decimal
T0 = et(date(2026, 11, 9), 10)
MIN = timedelta(minutes=1)
LAG = timedelta(minutes=15)


def sim_with_fills(*delays: timedelta) -> tuple[SimBroker, SimClock]:
    clock = SimClock(T0)
    sim = SimBroker(clock, SimConfig())
    for i, delay in enumerate(delays):
        req = SubmitOrderRequest(new_client_order_id(), "AAPL", Side.BUY, D(1), OrderType.LIMIT,
                                 TimeInForce.DAY, D(100))  # fmt: skip
        sim.submit_order(req)
        sim.schedule_fill(req.client_order_id, D(1), D(100), after=(i + 1) * MIN,
                          activity_delay=delay)  # fmt: skip
    return sim, clock


def test_complete_window_requires_confirmed_tail_and_closure() -> None:
    sim, clock = sim_with_fills(timedelta(0), timedelta(0), timedelta(0))
    clock.advance(30 * MIN)
    w = collect_window(sim, after=T0, until=T0 + 10 * MIN, now=clock.now(), posting_lag=LAG,
                       page_size=2)  # fmt: skip
    assert (len(w.activities), w.pages, w.tail_confirmed, w.closed, w.complete) == (
        3, 2, True, True, True)  # fmt: skip


def test_fully_paginated_but_unclosed_window_is_not_complete() -> None:
    sim, clock = sim_with_fills(timedelta(0), timedelta(0))
    clock.advance(5 * MIN)
    w = collect_window(sim, after=T0, until=clock.now(), now=clock.now(), posting_lag=LAG)
    assert w.tail_confirmed and not w.closed and not w.complete  # cursor exhausted != coverage
    assert len(w.activities) == 2


def test_late_posted_activity_within_lag_is_caught_by_closure() -> None:
    sim, clock = sim_with_fills(timedelta(minutes=10))  # executes T0+1m, posts at T0+11m
    clock.advance(5 * MIN)
    early = collect_window(sim, after=T0, until=T0 + 5 * MIN, now=clock.now(), posting_lag=LAG)
    assert early.activities == () and not early.complete  # empty, but not closed yet
    clock.advance(20 * MIN)
    later = collect_window(sim, after=T0, until=T0 + 5 * MIN, now=clock.now(), posting_lag=LAG)
    assert later.complete and len(later.activities) == 1


def test_posting_later_than_the_lag_is_found_by_overlap_rescan() -> None:
    """If the broker posts later than the assumed lag (a contract-test item), a window
    can be 'complete' without it; the overlapping re-scan still finds it, idempotently."""
    sim, clock = sim_with_fills(timedelta(0), timedelta(minutes=40))
    clock.advance(20 * MIN)
    first = collect_window(sim, after=T0, until=T0 + 4 * MIN, now=clock.now(), posting_lag=LAG)
    assert first.complete and len(first.activities) == 1
    clock.advance(30 * MIN)
    rescan = collect_window(sim, after=T0, until=T0 + 30 * MIN, now=clock.now(),
                            posting_lag=LAG)  # fmt: skip
    merged = merge_activities(first.activities, rescan.activities)
    assert len(merged) == 2


class FlakyPort:
    def __init__(self, pages: list[tuple[BrokerActivity, ...]], fail_on: int | None) -> None:
        self.pages, self.fail_on, self.calls = pages, fail_on, 0

    def get_activities(self, **_: Any) -> ActivityPage:
        self.calls += 1
        if self.calls == self.fail_on:
            raise ServiceUnavailableError("503")
        return ActivityPage(self.pages.pop(0) if self.pages else (), page_size=2)


def act(aid: str) -> BrokerActivity:
    return BrokerActivity(aid, "FEE", date(2026, 11, 9), None, f'{{"id":"{aid}"}}',
                          net_amount=D("-1"))  # fmt: skip


def test_interrupted_pagination_is_incomplete_with_partial_data() -> None:
    port = FlakyPort([(act("a"), act("b")), (act("c"),)], fail_on=2)
    with pytest.raises(IncompleteRetrievalError, match="interrupted on page 2") as info:
        collect_window(port, after=None, until=T0, now=T0 + LAG, posting_lag=LAG)  # type: ignore[arg-type]
    assert [a.activity_id for a in info.value.partial] == ["a", "b"]  # type: ignore[attr-defined]


def test_page_cap_and_bad_arguments() -> None:
    endless = FlakyPort([(act(f"x{i}"), act(f"y{i}")) for i in range(10)], fail_on=None)
    with pytest.raises(IncompleteRetrievalError, match="exceeded 3 pages"):
        collect_window(endless, after=None, until=T0, now=T0 + LAG, posting_lag=LAG,  # type: ignore[arg-type]
                       max_pages=3)  # fmt: skip
    with pytest.raises(ValueError, match="after < until"):
        collect_window(endless, after=T0, until=T0, now=T0, posting_lag=LAG)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        collect_window(endless, after=None, until=T0, now=T0, posting_lag=-LAG)  # type: ignore[arg-type]


# --------------------------------------------------------- durable coverage marker


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.create(tmp_path / "cov.sqlite3", RuntimeMode.SIMULATE)
    yield database
    database.close()


def test_coverage_marker_moves_only_forward_for_complete_contiguous_windows(db: Database) -> None:
    def advance(after: Any, until: Any, complete: bool) -> bool:
        with db.transaction() as tx:
            return advance_coverage(tx, after=after, until=until, complete=complete, at=T0)

    assert not advance(None, T0, complete=False)  # incomplete: never recorded
    with db.read() as s:
        assert complete_through(s) is None
    assert advance(None, T0, complete=True)
    assert not advance(T0 - MIN, T0 - 30 * MIN + MIN, complete=True)  # not beyond marker
    assert not advance(T0 + MIN, T0 + 10 * MIN, complete=True)  # gap after the marker
    assert advance(T0 - 5 * MIN, T0 + 10 * MIN, complete=True)  # overlapping: moves
    with db.read() as s:
        assert complete_through(s) == T0 + 10 * MIN
