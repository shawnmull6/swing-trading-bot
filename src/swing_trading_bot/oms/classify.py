"""Classification of non-trade broker activities (ARCHITECTURE §8.2, tightened in §12.18).

Every confirmed broker activity is preserved in ``broker_activities``. Only its
*attribution* decides whether it may touch the virtual ledger:

* ``ATTRIBUTABLE`` — evidence ties it to the bot's own trading; it is booked.
* ``EXTERNAL`` — explained and not the bot's: interest (``INT``) on the account's cash
  outside the $5,000 allocation. Recorded, never booked.
* ``UNEXPLAINED`` — anything not proven either way. Recorded, never booked; gates block
  new entries and reconciliation reports a MISMATCH for an operator to resolve.

Uncertain attribution is always UNEXPLAINED. Timing alone (a fee on a day the bot
traded, a dividend on a symbol the bot holds) is never sufficient.

**FEE** is attributable only when ALL hold:

1. it references an order (``order_id``) that is a bot order in the ledger (not an
   orphan) with at least one booked execution;
2. it is a charge (negative amount);
3. that order's booked fills carry no fee of their own (the fee is not already booked
   through the fill representation — no double booking);
4. no other attributable fee for the same order has the same amount and description
   (a second representation of the same fee is never booked twice).

**DIV** is never attributable in M1. Entitlement is decided by the shares held at the
ex-dividend (record) date, and neither Alpaca's DIV activity nor any source wired into
M1 reports a *verified* ex-date with the eligible quantity. Holding the symbol on the
payment date proves nothing (the position may have been bought after the ex-date or
sold before payment). A dividend is therefore preserved, never booked, and left
UNEXPLAINED ("entitlement unverified"): gates block new entries and reconciliation
reports ``DIVIDEND_ENTITLEMENT_UNVERIFIED`` for the operator. Attribution needs, before
M2, a verified per-symbol ex-date, the eligible quantity at that date and the per-share
amount (see ARCHITECTURE §12.19).

FILL activities are attributed by order (see :mod:`swing_trading_bot.oms.sync`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Final

from swing_trading_bot.broker.models import BrokerActivity
from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.domain.enums import ActivityAttribution
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.db import Session

EXTERNAL_TYPES: Final = frozenset({"INT"})

A = ActivityAttribution


@dataclass(frozen=True, slots=True)
class Classification:
    attribution: ActivityAttribution
    reason: str


def _when(a: BrokerActivity) -> datetime:
    return a.occurred_at or datetime.combine(a.activity_date, time(23, 59), tzinfo=MARKET_TZ)


def classify_cash_activity(s: Session, a: BrokerActivity) -> Classification:
    if a.activity_type in EXTERNAL_TYPES:
        return Classification(A.EXTERNAL, f"{a.activity_type}: account-level, not the bot's")
    if a.activity_type == "FEE":
        return _fee(s, a)
    if a.activity_type == "DIV":
        return _dividend(s, a)
    return Classification(A.UNEXPLAINED, f"{a.activity_type}: unclassified cash activity")


def _fee(s: Session, a: BrokerActivity) -> Classification:  # noqa: PLR0911
    if a.order_id is None:
        return Classification(A.UNEXPLAINED, "fee without an order reference")
    order = s.fetch_one(
        "SELECT client_order_id, status FROM orders WHERE broker_order_id = ?", (a.order_id,)
    )
    if order is None or str(order[1]) == "ORPHAN":
        return Classification(A.UNEXPLAINED, "fee for an order that is not a bot order")
    coid = str(order[0])
    fills = s.fetch_all("SELECT fees FROM fills WHERE client_order_id = ?", (coid,))
    if not fills:
        return Classification(A.UNEXPLAINED, "fee for a bot order without executions")
    if a.net_amount is None or a.net_amount >= ZERO:
        return Classification(A.UNEXPLAINED, "fee is not a charge")
    if any(Decimal(str(r[0])) != ZERO for r in fills):
        return Classification(A.UNEXPLAINED, "order's fills already carry fees (double booking)")
    twins = s.fetch_all(
        "SELECT raw_json FROM broker_activities WHERE activity_type = 'FEE' "
        "AND json_extract(raw_json, '$.order_id') = ? AND attribution = 'ATTRIBUTABLE' "
        "AND activity_id <> ?",
        (a.order_id, a.activity_id),
    )
    for (raw,) in twins:
        other = json.loads(str(raw))
        same_amount = Decimal(str(other.get("net_amount"))) == a.net_amount
        if same_amount and other.get("description") == a.description:
            return Classification(A.UNEXPLAINED, "same fee already booked for this order")
    return Classification(A.ATTRIBUTABLE, f"fee for bot order {coid}")


def _dividend(_s: Session, a: BrokerActivity) -> Classification:
    del a  # no verified entitlement evidence exists in M1, whatever the payload says
    return Classification(A.UNEXPLAINED, "dividend entitlement unverified (no verified "
                          "ex-date and eligible quantity)")  # fmt: skip


__all__ = [
    "EXTERNAL_TYPES",
    "Classification",
    "classify_cash_activity",
]
