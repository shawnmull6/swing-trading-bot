"""Step 9 refinement 2: activity windows establish coverage; cursors never do."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.activity_feed import (
    DEFAULT_RESCAN_POLICY,
    collect_window,
    merge_activities,
    rescans_due,
)
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


# --------------------------- posting lag is an assumption, not a guarantee (step 10 fix)


def test_post_beyond_the_lag_is_missed_until_a_historical_rescan(db: Database) -> None:
    """A fill executed at T0+1m but posted 40 minutes later (beyond the assumed 15-minute
    lag) falls inside a window already marked complete. Forward windows never see it;
    only a historical re-scan that reaches back over it does."""
    sim, clock = sim_with_fills(timedelta(minutes=40))
    seen: dict[str, BrokerActivity] = {}

    clock.advance_to(T0 + 20 * MIN)
    w1 = collect_window(sim, after=None, until=T0 + 5 * MIN, now=clock.now(), posting_lag=LAG)
    assert w1.complete and w1.activities == ()  # "complete" -- but the fill is not posted yet
    with db.transaction() as tx:
        assert advance_coverage(tx, after=w1.after, until=w1.until, complete=w1.complete,
                                at=clock.now())  # fmt: skip
    clock.advance_to(T0 + 60 * MIN)  # the fill has now been posted
    w2 = collect_window(sim, after=w1.until, until=T0 + 45 * MIN, now=clock.now(),
                        posting_lag=LAG)  # fmt: skip
    seen.update((a.activity_id, a) for a in (*w1.activities, *w2.activities))
    assert w2.complete and seen == {}  # forward progress alone misses it

    windows = rescans_due(DEFAULT_RESCAN_POLICY, now=clock.now(),
                          complete_through=w2.until, last_periodic_rescan=None,
                          baseline_at=None)  # fmt: skip
    assert [w.kind for w in windows] == ["OVERLAP", "PERIODIC"]
    for planned in windows:
        found = collect_window(sim, after=planned.after, until=planned.until, now=clock.now(),
                               posting_lag=LAG)  # fmt: skip
        seen.update((a.activity_id, a) for a in found.activities)
    assert len(seen) == 1  # found only by re-reading history


def test_overlap_shorter_than_the_delay_still_misses_it() -> None:
    sim, clock = sim_with_fills(timedelta(hours=3))  # posted 3 hours late
    clock.advance_to(T0 + 4 * timedelta(hours=1))
    overlap_only = collect_window(
        sim,
        after=T0 + timedelta(hours=2),
        until=clock.now() - LAG,
        now=clock.now(),
        posting_lag=LAG,
    )
    assert overlap_only.activities == ()
    longer = collect_window(sim, after=T0 - timedelta(days=1), until=clock.now() - LAG,
                            now=clock.now(), posting_lag=LAG)  # fmt: skip
    assert len(longer.activities) == 1


def test_rescan_policy_validation_and_planning() -> None:
    p = DEFAULT_RESCAN_POLICY
    assert p.overlap >= p.posting_lag and p.periodic_lookback > p.overlap
    with pytest.raises(ValueError, match="overlap"):
        replace(p, overlap=timedelta(minutes=5))
    with pytest.raises(ValueError, match="further back"):
        replace(p, periodic_lookback=timedelta(minutes=30))
    with pytest.raises(ValueError, match="periodic_every"):
        replace(p, periodic_every=timedelta(0))
    now = T0 + timedelta(hours=2)
    recent = rescans_due(
        p, now=now, complete_through=now - LAG,
        last_periodic_rescan=now - timedelta(hours=1), baseline_at=None,
    )  # fmt: skip
    assert [w.kind for w in recent] == ["OVERLAP"]  # periodic not due yet
    full = rescans_due(p, now=now, complete_through=None, last_periodic_rescan=now,
                       baseline_at=T0 - timedelta(days=30), reconciling=True)  # fmt: skip
    assert [(w.kind, w.after) for w in full] == [("FULL_HISTORY", T0 - timedelta(days=30))]
    assert all(w.until == now - p.posting_lag for w in full)
