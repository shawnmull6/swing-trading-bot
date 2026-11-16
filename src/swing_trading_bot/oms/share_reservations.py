"""Sell-side share reservations and cancel/replace readiness (M2-3, ARCHITECTURE §12.38).

Share reservations are **derived**, not stored: every working SELL order holds
``qty - booked filled_qty`` shares of its symbol, computed from the ``orders`` table that
already exists (no schema change). This mirrors the broker's ``qty_available`` (§9, V9)
and is deliberately conservative:

* a working sell holds its unfilled shares in *every* non-terminal status, including
  ``PENDING_SUBMIT`` / ``SUBMIT_AMBIGUOUS`` (it may exist at the broker) and
  ``PENDING_CANCEL`` (a cancel request frees nothing; only a broker-confirmed terminal
  status, applied after all fills are booked, does);
* broker-reported fills whose activities are not booked yet keep their shares held: the
  ledger position still contains them, and so does the hold, so the two stay consistent;
* terminal orders (including ``CONFIRMED_ABSENT``, which only an operator can set, and
  ``ORPHAN``, whose fills are already booked) hold nothing — **except** shares the broker
  reports filled but the ledger has not booked (``broker_filled_qty > filled_qty``). The
  lifecycle never makes such an order terminal, but the hold does not rely on that: the
  shares are already sold at the broker and still counted in the ledger position.

Nothing here writes or calls a broker; the OrderManager evaluates the book inside its
``BEGIN IMMEDIATE`` intent transaction, so two exit intents cannot both spend the same
shares (the first commit is visible to the second).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from swing_trading_bot.domain.enums import OrderRole, OrderStatus, Side
from swing_trading_bot.domain.money import ZERO, is_whole
from swing_trading_bot.domain.state_machine import AMBIGUOUS_STATUSES, is_terminal
from swing_trading_bot.ledger.repositories import OrderRecord


class SellRefusal(StrEnum):
    INVALID_QTY = "INVALID_QTY"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    """The new sell plus every existing hold would exceed the position (oversell)."""
    PLAN_CONFLICT = "PLAN_CONFLICT"
    """A working order for the symbol belongs to another plan, or to no plan."""
    ROLE_ALREADY_LIVE = "ROLE_ALREADY_LIVE"
    """At most one live PROTECTIVE_STOP and one live EXIT per plan (§9)."""
    INVALID_ROLE = "INVALID_ROLE"
    ENTRY_STILL_WORKING = "ENTRY_STILL_WORKING"
    UNBOOKED_FILLS = "UNBOOKED_FILLS"
    BOOK_INCONSISTENT = "BOOK_INCONSISTENT"


_SELL_ROLES = frozenset({OrderRole.PROTECTIVE_STOP, OrderRole.TARGET, OrderRole.EXIT})
_SINGLE_LIVE_ROLES = frozenset({OrderRole.PROTECTIVE_STOP, OrderRole.EXIT})


def held_shares(order: OrderRecord) -> Decimal:
    """Shares a single order holds against its symbol's position."""
    if order.side is not Side.SELL:
        return ZERO
    if is_terminal(order.status):
        return order.unbooked_fill_qty
    return max(ZERO, order.qty - order.filled_qty)


@dataclass(frozen=True, slots=True)
class ShareHold:
    client_order_id: str
    trade_plan_id: str | None
    role: OrderRole | None
    status: OrderStatus
    held_qty: Decimal
    uncertain: bool
    """Ambiguous submission, pending cancel, or unbooked broker fills."""


@dataclass(frozen=True, slots=True)
class SellSideBook:
    symbol: str
    position_qty: Decimal
    holds: tuple[ShareHold, ...]
    working_buys: tuple[OrderRecord, ...]
    unbooked: tuple[str, ...]

    @classmethod
    def build(
        cls, symbol: str, position_qty: Decimal, orders: Iterable[OrderRecord]
    ) -> SellSideBook:
        holds: list[ShareHold] = []
        buys: list[OrderRecord] = []
        unbooked: list[str] = []
        for o in orders:
            terminal = is_terminal(o.status)
            if o.symbol != symbol or (terminal and o.unbooked_fill_qty == ZERO):
                continue
            if o.unbooked_fill_qty > ZERO:
                unbooked.append(o.client_order_id)
            if o.side is Side.BUY:
                if not terminal:
                    buys.append(o)
                continue
            uncertain = (
                o.status in AMBIGUOUS_STATUSES
                or o.status is OrderStatus.PENDING_CANCEL
                or o.unbooked_fill_qty > ZERO
            )
            holds.append(ShareHold(o.client_order_id, o.trade_plan_id, o.order_role, o.status,
                                   held_shares(o), uncertain))  # fmt: skip
        return cls(symbol, position_qty, tuple(holds), tuple(buys), tuple(unbooked))

    # ---------------------------------------------------------------- derived
    @property
    def reserved(self) -> Decimal:
        return sum((h.held_qty for h in self.holds), ZERO)

    @property
    def available(self) -> Decimal:
        return max(ZERO, self.position_qty - self.reserved)

    @property
    def uncertain(self) -> bool:
        return any(h.uncertain for h in self.holds)

    def violations(self) -> tuple[str, ...]:
        """Book states that must never exist (callers halt or refuse)."""
        out: list[str] = []
        if self.position_qty < ZERO:
            out.append(f"negative position {self.position_qty}")
        if self.reserved > self.position_qty:
            out.append(f"sell holds {self.reserved} exceed position {self.position_qty}")
        plans = {h.trade_plan_id for h in self.holds} | {b.trade_plan_id for b in self.working_buys}
        if len(plans) > 1:
            out.append(f"working orders span several plans: {sorted(map(str, plans))}")
        for role in _SINGLE_LIVE_ROLES:
            n = sum(1 for h in self.holds if h.role is role)
            if n > 1:
                out.append(f"{n} live {role.value} orders")
        return tuple(out)

    def check_new_sell(
        self, qty: Decimal, *, trade_plan_id: str, role: OrderRole
    ) -> tuple[SellRefusal, ...]:
        """Refusals for a new plan sell of ``qty`` (empty tuple = permitted by the book)."""
        out: set[SellRefusal] = set()
        if qty <= ZERO or not is_whole(qty):
            out.add(SellRefusal.INVALID_QTY)
        if role not in _SELL_ROLES:
            out.add(SellRefusal.INVALID_ROLE)
        if self.violations():
            out.add(SellRefusal.BOOK_INCONSISTENT)
        if self.unbooked:
            out.add(SellRefusal.UNBOOKED_FILLS)
        if self.working_buys:
            out.add(SellRefusal.ENTRY_STILL_WORKING)
        if any(h.trade_plan_id != trade_plan_id for h in self.holds) or any(
            b.trade_plan_id != trade_plan_id for b in self.working_buys
        ):
            out.add(SellRefusal.PLAN_CONFLICT)
        if role in _SINGLE_LIVE_ROLES and any(h.role is role for h in self.holds):
            out.add(SellRefusal.ROLE_ALREADY_LIVE)
        if qty > ZERO and self.reserved + qty > self.position_qty:
            out.add(SellRefusal.INSUFFICIENT_SHARES)
        return tuple(sorted(out))


# ============================================================ cancel/replace


class ReplaceReadiness(StrEnum):
    """Whether a working exit order may be superseded by a new one.

    A replacement is always *cancel, then confirm, then place*: the new order is created
    only once the old one is broker-confirmed terminal with every fill booked, and its
    quantity is recomputed from the book at that point. There is no atomic replace, so a
    position is briefly without that order; whether that is acceptable for a protective
    stop is an unresolved policy (ARCHITECTURE §12.38).
    """

    CANCEL_NOT_REQUESTED = "CANCEL_NOT_REQUESTED"
    AWAITING_CANCEL_CONFIRMATION = "AWAITING_CANCEL_CONFIRMATION"
    AWAITING_FILL_BOOKING = "AWAITING_FILL_BOOKING"
    OLD_ORDER_UNRESOLVED = "OLD_ORDER_UNRESOLVED"
    OLD_ORDER_FILLED = "OLD_ORDER_FILLED"
    READY = "READY"


def replace_readiness(old: OrderRecord) -> ReplaceReadiness:
    if old.unbooked_fill_qty > ZERO:
        return ReplaceReadiness.AWAITING_FILL_BOOKING
    status = old.status
    if status in AMBIGUOUS_STATUSES or status is OrderStatus.ORPHAN:
        return ReplaceReadiness.OLD_ORDER_UNRESOLVED
    if status is OrderStatus.PENDING_CANCEL:
        return ReplaceReadiness.AWAITING_CANCEL_CONFIRMATION
    if status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
        return ReplaceReadiness.CANCEL_NOT_REQUESTED
    if status is OrderStatus.FILLED:
        return ReplaceReadiness.OLD_ORDER_FILLED
    return ReplaceReadiness.READY  # CANCELED, EXPIRED, REJECTED, CONFIRMED_ABSENT


__all__ = [
    "ReplaceReadiness",
    "SellRefusal",
    "SellSideBook",
    "ShareHold",
    "held_shares",
    "replace_readiness",
]
