"""Ledger invariant-checking infrastructure (ARCHITECTURE §6.5, §12.3).

Invariants are evaluated **after** a write transaction commits. They never block a
write: a broker-confirmed event is always recorded first, then a violation is
reported so the application layer can set a persistent ``LEDGER_INVARIANT`` halt.

Step 4 ships the data-consistency invariants that need no accounting logic. Accounting
invariants (cash = journal, available cash >= 0, deployed capital <= ceiling,
cost-basis replay) are registered by step 6 through the same interface.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from swing_trading_bot.domain.enums import OrderStatus, Side
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.domain.state_machine import TERMINAL_STATUSES
from swing_trading_bot.ledger.codec import dec_from_db, int_from_db, str_from_db
from swing_trading_bot.ledger.db import Session


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: str
    subject: str
    detail: str


class Invariant(Protocol):
    @property
    def name(self) -> str: ...

    def check(self, s: Session) -> list[Violation]: ...


@dataclass(frozen=True, slots=True)
class InvariantReport:
    checked: tuple[str, ...]
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


class InvariantChecker:
    def __init__(self, invariants: Iterable[Invariant]) -> None:
        self._invariants: tuple[Invariant, ...] = tuple(invariants)
        names = [i.name for i in self._invariants]
        if len(names) != len(set(names)):
            raise ValueError("invariant names must be unique")

    def with_additional(self, invariants: Iterable[Invariant]) -> InvariantChecker:
        return InvariantChecker((*self._invariants, *invariants))

    def run(self, s: Session) -> InvariantReport:
        violations: list[Violation] = []
        for invariant in self._invariants:
            try:
                violations.extend(invariant.check(s))
            except Exception as exc:  # noqa: BLE001 - a failing check is itself a violation
                violations.append(
                    Violation(invariant.name, "checker", f"check raised {type(exc).__name__}")
                )
        return InvariantReport(
            checked=tuple(i.name for i in self._invariants), violations=tuple(violations)
        )


class OrderFillConsistency:
    """order.filled_qty equals the sum of its fills and does not exceed order.qty."""

    name = "order_fill_consistency"

    def check(self, s: Session) -> list[Violation]:
        fills: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for coid, qty in s.fetch_all("SELECT client_order_id, qty FROM fills"):
            fills[str_from_db(coid)] += dec_from_db(qty)
        out: list[Violation] = []
        for coid_raw, qty_raw, filled_raw in s.fetch_all(
            "SELECT client_order_id, qty, filled_qty FROM orders"
        ):
            coid = str_from_db(coid_raw)
            qty, filled = dec_from_db(qty_raw), dec_from_db(filled_raw)
            summed = fills.get(coid, ZERO)
            if filled != summed:
                out.append(Violation(self.name, coid, f"filled_qty {filled} != fills {summed}"))
            if summed > qty:
                out.append(Violation(self.name, coid, f"fills {summed} exceed order qty {qty}"))
        return out


class ReservationConsistency:
    """Terminal orders hold no ACTIVE reservation; ACTIVE reservations need a working order."""

    name = "reservation_consistency"

    def check(self, s: Session) -> list[Violation]:
        rows = s.fetch_all(
            "SELECT r.client_order_id, r.status, o.status FROM cash_reservations r "
            "JOIN orders o ON o.client_order_id = r.client_order_id"
        )
        out: list[Violation] = []
        for coid, r_status, o_status in rows:
            terminal = OrderStatus(str_from_db(o_status)) in TERMINAL_STATUSES
            active = str_from_db(r_status) == "ACTIVE"
            if terminal and active:
                out.append(
                    Violation(
                        self.name,
                        str(coid),
                        f"terminal order ({o_status}) holds ACTIVE reservation",
                    )
                )
            if not terminal and not active:
                out.append(
                    Violation(
                        self.name, str(coid), f"working order ({o_status}) has a closed reservation"
                    )
                )
        return out


class PositionReplay:
    """Materialised position quantities equal the replay of fills (qty only).

    Cost-basis replay is an accounting invariant added in step 6.
    """

    name = "position_qty_replay"

    def check(self, s: Session) -> list[Violation]:
        net: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
        for pid, symbol, side, qty in s.fetch_all(
            "SELECT o.portfolio_id, f.symbol, f.side, f.qty FROM fills f "
            "JOIN orders o ON o.client_order_id = f.client_order_id"
        ):
            q = dec_from_db(qty)
            key = (int_from_db(pid), str_from_db(symbol))
            net[key] += q if Side(str_from_db(side)) is Side.BUY else -q
        stored: dict[tuple[int, str], Decimal] = {
            (int_from_db(pid), str_from_db(sym)): dec_from_db(qty)
            for pid, sym, qty in s.fetch_all("SELECT portfolio_id, symbol, qty FROM positions")
        }
        out: list[Violation] = []
        for key in sorted(set(net) | set(stored)):
            expected = net.get(key, ZERO)
            actual = stored.get(key, ZERO)
            if expected != actual:
                out.append(
                    Violation(
                        self.name, f"{key[0]}:{key[1]}", f"position {actual} != fills {expected}"
                    )
                )
        return out


class NonNegativePositions:
    name = "non_negative_positions"

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for pid, symbol, qty in s.fetch_all("SELECT portfolio_id, symbol, qty FROM positions"):
            q = dec_from_db(qty)
            if q < ZERO:
                out.append(Violation(self.name, f"{pid}:{symbol}", f"negative position {q}"))
        return out


class SingletonRows:
    name = "singleton_rows"

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for table in ("database_identity", "trading_state"):
            count = s.scalar(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
            if count != 1:
                out.append(Violation(self.name, table, f"expected 1 row, found {count}"))
        return out


DATA_INVARIANTS: Sequence[Invariant] = (
    SingletonRows(),
    OrderFillConsistency(),
    ReservationConsistency(),
    PositionReplay(),
    NonNegativePositions(),
)


def default_checker() -> InvariantChecker:
    return InvariantChecker(DATA_INVARIANTS)
