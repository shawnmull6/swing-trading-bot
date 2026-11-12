"""Broker-agnostic activity retrieval over time windows (ARCHITECTURE §8.4, §12.11).

Activity ids are **opaque**: nothing here compares, sorts or parses them; the cursor a
page returns is passed back verbatim.

Coverage model (the durable strategy future synchronization must follow)
-------------------------------------------------------------------------
Coverage is established by **time windows**, never by a cursor. A window ``(after,
until]`` is *complete* only when both hold:

1. **tail confirmed** — pagination ended on an empty or short page (a full page is
   always followed by one more request); and
2. **closed** — ``until <= now - posting_lag``: activities can be posted after the fact
   with earlier timestamps (fees, dividends, corrections), so a window that has not
   aged past the posting lag can still change.

"Complete" is an **operational** statement, not a permanent guarantee. The posting
lag (assumed 15 minutes, unverified) is an assumption about typical broker behaviour:
a correction, fee or dividend posted *later* than the lag, with an earlier timestamp,
falls inside a window already marked complete and is invisible to the next forward
window. Only a later historical re-scan that reaches back over it can find it. Hence
:class:`RescanPolicy` and :func:`rescans_due`, and reconciliation over the whole period
since the active baseline wherever practical.

A cursor that simply advanced proves nothing about coverage. Any read failure,
non-advancing cursor, conflicting duplicate or page cap raises
:class:`IncompleteRetrievalError` carrying the activities read so far — they may be
ingested (ingestion is idempotent by activity id) but the window is not complete.

Synchronization (steps 12-13) therefore:

* reads consecutive windows whose ``after`` overlaps the previous complete window by at
  least the posting lag (overlap re-scan), deduplicating by id;
* ingests each window's activities and advances the durable coverage marker
  (``activities.complete_through``) in the **same** ledger transaction, and only for a
  complete window (``ledger.activity_coverage``);
* runs the historical re-scans of :class:`RescanPolicy` (configurable overlap, periodic
  longer lookbacks) and, in reconciliation, the whole period since the active
  baseline. Re-scans never move the coverage marker backwards; anything they find
  that is older than the marker is ingested as a late activity and flagged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from swing_trading_bot.broker.errors import (
    BrokerError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
)
from swing_trading_bot.broker.models import BrokerActivity
from swing_trading_bot.broker.ports import MAX_ACTIVITY_PAGE_SIZE, BrokerReadPort


def merge_activities(*batches: Iterable[BrokerActivity]) -> list[BrokerActivity]:
    """Union of activity batches in first-seen order, deduplicated by opaque id."""
    seen: dict[str, BrokerActivity] = {}
    for batch in batches:
        for activity in batch:
            existing = seen.get(activity.activity_id)
            if existing is None:
                seen[activity.activity_id] = activity
            elif existing.raw_json != activity.raw_json:
                raise InvalidBrokerDataError(
                    f"activity {activity.activity_id} returned twice with different content"
                )
    return list(seen.values())


@dataclass(frozen=True, slots=True)
class ActivityWindow:
    after: datetime | None
    until: datetime
    activities: tuple[BrokerActivity, ...]
    pages: int
    tail_confirmed: bool
    closed: bool

    @property
    def complete(self) -> bool:
        """Coverage may be advanced to ``until`` only when this is True."""
        return self.tail_confirmed and self.closed


def collect_window(
    port: BrokerReadPort,
    *,
    after: datetime | None,
    until: datetime,
    now: datetime,
    posting_lag: timedelta,
    page_size: int = MAX_ACTIVITY_PAGE_SIZE,
    max_pages: int = 500,
) -> ActivityWindow:
    """Read every activity in ``(after, until]`` and say whether coverage is complete."""
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    if until.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("window bounds must be timezone-aware")
    if after is not None and after >= until:
        raise ValueError("window must have after < until")
    if posting_lag < timedelta(0):
        raise ValueError("posting_lag must not be negative")
    closed = until <= now - posting_lag
    token: str | None = None
    pages: list[tuple[BrokerActivity, ...]] = []
    seen_ids: set[str] = set()

    def partial() -> tuple[BrokerActivity, ...]:
        try:
            return tuple(merge_activities(*pages))
        except InvalidBrokerDataError:  # pragma: no cover - conflicts raised earlier
            return ()

    for count in range(1, max_pages + 1):
        try:
            page = port.get_activities(
                page_token=token, page_size=page_size, after=after, until=until
            )
        except BrokerError as exc:
            raise IncompleteRetrievalError(
                f"activity window interrupted on page {count}: {type(exc).__name__}",
                partial=partial(),
            ) from exc
        if not page.activities:
            return ActivityWindow(
                after, until, partial(), count, tail_confirmed=True, closed=closed
            )
        new_ids = [a.activity_id for a in page.activities]
        if len(set(new_ids)) != len(new_ids):
            merge_activities(page.activities)  # raises on conflicting duplicates
        if seen_ids.issuperset(new_ids):
            raise IncompleteRetrievalError("activity pagination made no progress",
                                           partial=partial())  # fmt: skip
        seen_ids.update(new_ids)
        pages.append(page.activities)
        merged = merge_activities(*pages)
        if not page.possibly_more:
            return ActivityWindow(
                after, until, tuple(merged), count, tail_confirmed=True, closed=closed
            )
        next_token = page.last_id
        if next_token is None or next_token == token:
            raise IncompleteRetrievalError("activity pagination cursor did not advance",
                                           partial=tuple(merged))  # fmt: skip
        token = next_token
    raise IncompleteRetrievalError(f"activity window exceeded {max_pages} pages",
                                   partial=partial())  # fmt: skip


def collect_activities(
    port: BrokerReadPort,
    *,
    after: datetime | None,
    until: datetime | None,
    page_size: int = MAX_ACTIVITY_PAGE_SIZE,
    max_pages: int = 500,
) -> list[BrokerActivity]:
    """Every activity the port returns for ``(after, until]``. This establishes NO
    coverage (use :func:`collect_window` for that); it is a convenience for re-scans."""
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    token: str | None = None
    pages: list[tuple[BrokerActivity, ...]] = []
    seen_ids: set[str] = set()
    for _ in range(max_pages):
        page = port.get_activities(page_token=token, page_size=page_size, after=after, until=until)
        if not page.activities:
            return merge_activities(*pages)
        new_ids = [a.activity_id for a in page.activities]
        if len(set(new_ids)) != len(new_ids):
            merge_activities(page.activities)  # raises on conflicting duplicates
        if seen_ids.issuperset(new_ids):
            raise InvalidBrokerDataError("activity pagination made no progress")
        seen_ids.update(new_ids)
        pages.append(page.activities)
        if not page.possibly_more:
            return merge_activities(*pages)
        next_token = page.last_id
        if next_token is None or next_token == token:
            raise InvalidBrokerDataError("activity pagination cursor did not advance")
        token = next_token
    raise InvalidBrokerDataError(f"activity window exceeded {max_pages} pages")


# ---------------------------------------------------------------- historical re-scans


@dataclass(frozen=True, slots=True)
class RescanPolicy:
    """How far back synchronization re-reads already-covered history.

    * ``overlap`` — every forward window reaches back this far (>= posting lag);
    * ``periodic_lookback`` / ``periodic_every`` — a longer re-scan run on a schedule
      (e.g. 5 trading days, daily) to catch postings later than the lag;
    * ``full_history`` — re-read everything since the active broker baseline during
      reconciliation, when practical (bounded by broker retention and rate limits).

    Values are operational choices to be tuned with contract-test evidence; none of them
    makes coverage permanent.
    """

    posting_lag: timedelta
    overlap: timedelta
    periodic_lookback: timedelta
    periodic_every: timedelta
    full_history: bool = True

    def __post_init__(self) -> None:
        if self.posting_lag < timedelta(0):
            raise ValueError("posting_lag must not be negative")
        if self.overlap < self.posting_lag:
            raise ValueError("overlap must be at least the posting lag")
        if self.periodic_lookback <= self.overlap:
            raise ValueError("periodic lookback must reach further back than the overlap")
        if self.periodic_every <= timedelta(0):
            raise ValueError("periodic_every must be positive")


DEFAULT_RESCAN_POLICY = RescanPolicy(
    posting_lag=timedelta(minutes=15),
    overlap=timedelta(hours=1),
    periodic_lookback=timedelta(days=7),
    periodic_every=timedelta(days=1),
)


@dataclass(frozen=True, slots=True)
class RescanWindow:
    kind: str  # "OVERLAP" | "PERIODIC" | "FULL_HISTORY"
    after: datetime | None
    until: datetime


def rescans_due(
    policy: RescanPolicy,
    *,
    now: datetime,
    complete_through: datetime | None,
    last_periodic_rescan: datetime | None,
    baseline_at: datetime | None,
    reconciling: bool = False,
) -> list[RescanWindow]:
    """Windows synchronization should (re-)read now. Pure planning; reads nothing."""
    until = now - policy.posting_lag
    windows: list[RescanWindow] = []
    if complete_through is not None:
        windows.append(RescanWindow("OVERLAP", complete_through - policy.overlap, until))
    if last_periodic_rescan is None or now - last_periodic_rescan >= policy.periodic_every:
        windows.append(RescanWindow("PERIODIC", now - policy.periodic_lookback, until))
    if reconciling and policy.full_history:
        windows.append(RescanWindow("FULL_HISTORY", baseline_at, until))
    return windows
