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
* re-scans from the start of the previous session daily and, in reconciliation, the
  whole period since the active baseline.
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
