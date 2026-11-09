"""Accounting invariants (ARCHITECTURE §6.5, §12.3), registered on top of the step 4
data invariants. They never block a write; violations lead to a persistent
``LEDGER_INVARIANT`` halt by the portfolio service after the transaction commits.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from swing_trading_bot.domain.accounting import (
    PositionState,
    apply_buy,
    apply_sell,
    buy_cost,
    sell_proceeds,
)
from swing_trading_bot.domain.enums import Side
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.balances import compute_balances
from swing_trading_bot.ledger.codec import dec_from_db, int_from_db, str_from_db
from swing_trading_bot.ledger.db import Session
from swing_trading_bot.ledger.invariants import (
    Invariant,
    InvariantChecker,
    Violation,
    default_checker,
)
from swing_trading_bot.ledger.repositories import FillsRepository, OrdersRepository

TodayFn = Callable[[], date]


def _portfolio_ids(s: Session) -> list[int]:
    return [int_from_db(r[0]) for r in s.fetch_all("SELECT id FROM portfolios ORDER BY id")]


class InitialDeposit:
    """Exactly one DEPOSIT entry, equal to the portfolio's initial capital."""

    name = "initial_deposit"

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for pid in _portfolio_ids(s):
            rows = s.fetch_all(
                "SELECT amount FROM cash_entries WHERE portfolio_id = ? AND kind = 'DEPOSIT'",
                (pid,),
            )
            initial = dec_from_db(
                s.scalar("SELECT initial_capital FROM portfolios WHERE id = ?", (pid,))
            )
            amounts = [dec_from_db(r[0]) for r in rows]
            if amounts != [initial]:
                out.append(Violation(self.name, str(pid), f"deposits {amounts} != [{initial}]"))
        return out


class FillCashLinkage:
    """Every fill has exactly one BUY/SELL cash entry with the exact amount."""

    name = "fill_cash_linkage"

    def check(self, s: Session) -> list[Violation]:
        entries: dict[int, list[tuple[str, Decimal]]] = defaultdict(list)
        for fill_id, kind, amount in s.fetch_all(
            "SELECT fill_id, kind, amount FROM cash_entries WHERE fill_id IS NOT NULL"
        ):
            entries[int_from_db(fill_id)].append((str_from_db(kind), dec_from_db(amount)))
        out: list[Violation] = []
        for f in FillsRepository().list_in_application_order(s):
            if f.id is None:  # pragma: no cover - stored rows always have ids
                continue
            if f.side is Side.BUY:
                expected = [("BUY", -buy_cost(f.qty, f.price, f.fees))]
            else:
                expected = [("SELL", sell_proceeds(f.qty, f.price, f.fees))]
            if entries.get(f.id, []) != expected:
                out.append(
                    Violation(self.name, f.activity_id, f"cash {entries.get(f.id)} != {expected}")
                )
        return out


class PositionCostReplay:
    """Positions (qty, cost basis, realized P&L) equal an average-cost replay of fills."""

    name = "position_cost_replay"

    def check(self, s: Session) -> list[Violation]:
        orders = OrdersRepository()
        replay: dict[tuple[int, str], PositionState] = defaultdict(PositionState.flat)
        out: list[Violation] = []
        portfolio_of: dict[str, int] = {}
        for f in FillsRepository().list_in_application_order(s):
            pid = portfolio_of.get(f.client_order_id)
            if pid is None:
                pid = orders.require(s, f.client_order_id).portfolio_id
                portfolio_of[f.client_order_id] = pid
            key = (pid, f.symbol)
            if f.side is Side.BUY:
                replay[key] = apply_buy(replay[key], f.qty, f.price, f.fees)
            else:
                result = apply_sell(replay[key], f.qty, f.price, f.fees)
                replay[key] = result.position
                if f.realized_pnl != result.realized_pnl:
                    out.append(
                        Violation(self.name, f.activity_id,
                                  f"fill realized {f.realized_pnl} != replay "
                                  f"{result.realized_pnl}")
                    )  # fmt: skip
        stored = {
            (int_from_db(r[0]), str_from_db(r[1])): PositionState(
                dec_from_db(r[2]), dec_from_db(r[3]), dec_from_db(r[4])
            )
            for r in s.fetch_all(
                "SELECT portfolio_id, symbol, qty, cost_basis, realized_pnl FROM positions"
            )
        }
        for key in sorted(set(replay) | set(stored)):
            expected = replay.get(key, PositionState.flat())
            actual = stored.get(key, PositionState.flat())
            if expected != actual:
                out.append(
                    Violation(
                        self.name, f"{key[0]}:{key[1]}", f"stored {actual} != replay {expected}"
                    )
                )
        return out


class CashConservation:
    """cash + Σ cost basis == deposits + Σ realized + dividends + fee entries + adjustments.

    Holds exactly for every buy, sell, fee and dividend; any other movement of money
    (or a lost update) breaks it.
    """

    name = "cash_conservation"

    def __init__(self, today: TodayFn) -> None:
        self._today = today

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for pid in _portfolio_ids(s):
            b = compute_balances(s, pid, self._today())
            all_cost = sum(
                (dec_from_db(r[0]) for r in s.fetch_all(
                    "SELECT cost_basis FROM positions WHERE portfolio_id = ?", (pid,))),
                ZERO,
            )  # fmt: skip
            realized = sum(
                (dec_from_db(r[0]) for r in s.fetch_all(
                    "SELECT realized_pnl FROM positions WHERE portfolio_id = ?", (pid,))),
                ZERO,
            )  # fmt: skip
            lhs = b.cash + all_cost
            rhs = b.deposits + realized + b.dividends + b.fee_entries + b.adjustments
            if lhs != rhs:
                out.append(Violation(self.name, str(pid), f"cash+cost {lhs} != sources {rhs}"))
        return out


class CapitalLimits:
    """Cash and available cash non-negative; deployed capital within the fixed ceiling."""

    name = "capital_limits"

    def __init__(self, today: TodayFn) -> None:
        self._today = today

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for pid in _portfolio_ids(s):
            b = compute_balances(s, pid, self._today())
            if b.cash < ZERO:
                out.append(Violation(self.name, str(pid), f"negative cash {b.cash}"))
            if b.available_cash < ZERO:
                out.append(
                    Violation(self.name, str(pid), f"negative available cash {b.available_cash}")
                )
            if b.deployed_capital > b.max_capital_allocation:
                out.append(
                    Violation(
                        self.name,
                        str(pid),
                        f"deployed {b.deployed_capital} exceeds ceiling {b.max_capital_allocation}",
                    )
                )
        return out


class CashEntrySigns:
    """Sign conventions: DEPOSIT > 0, BUY < 0, FEE < 0, DIVIDEND > 0 (SELL may be any
    sign only in the pathological case fees > proceeds; ADJUSTMENT is signed)."""

    name = "cash_entry_signs"

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for entry_id, kind_raw, amount_raw in s.fetch_all(
            "SELECT id, kind, amount FROM cash_entries"
        ):
            kind, amount = str_from_db(kind_raw), dec_from_db(amount_raw)
            ok = {
                "DEPOSIT": amount > ZERO,
                "BUY": amount < ZERO,
                "FEE": amount < ZERO,
                "DIVIDEND": amount > ZERO,
            }.get(kind, True)
            if not ok:
                out.append(Violation(self.name, str(entry_id), f"{kind} amount {amount}"))
        return out


class ReservationBounds:
    name = "reservation_bounds"

    def check(self, s: Session) -> list[Violation]:
        out: list[Violation] = []
        for coid, original, remaining in s.fetch_all(
            "SELECT client_order_id, original, remaining FROM cash_reservations"
        ):
            o, r = dec_from_db(original), dec_from_db(remaining)
            if not ZERO <= r <= o:
                out.append(Violation(self.name, str(coid), f"remaining {r} outside [0, {o}]"))
        return out


def accounting_invariants(today: TodayFn) -> tuple[Invariant, ...]:
    return (
        InitialDeposit(),
        FillCashLinkage(),
        PositionCostReplay(),
        CashConservation(today),
        CapitalLimits(today),
        CashEntrySigns(),
        ReservationBounds(),
    )


def accounting_checker(today: TodayFn) -> InvariantChecker:
    """Step 4 data invariants plus step 6 accounting invariants."""
    return default_checker().with_additional(accounting_invariants(today))
