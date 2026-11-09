"""Broker-agnostic activity retrieval over time windows (ARCHITECTURE §8.4, step 9).

Activity ids are **opaque**: nothing here compares, sorts or parses them. The cursor
returned by a page is passed back verbatim. Retrieval is bounded and defensive:

* duplicates within a window (same id, identical content) are dropped; the same id
  with different content raises :class:`InvalidBrokerDataError`;
* a page that returns an id already seen in this window, or a cursor that does not
  advance, is treated as a pagination fault (never an infinite loop);
* ``max_pages`` bounds the work per window.

Overlapping windows can be merged with :func:`merge_activities`; ledger ingestion is
idempotent by activity id, so re-reading a window is always safe.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from swing_trading_bot.broker.errors import InvalidBrokerDataError
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


def collect_activities(
    port: BrokerReadPort,
    *,
    after: datetime | None,
    until: datetime | None,
    page_size: int = MAX_ACTIVITY_PAGE_SIZE,
    max_pages: int = 500,
) -> list[BrokerActivity]:
    """Read every activity in (after, until] by following the port's opaque cursor."""
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
