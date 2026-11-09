"""Cash and capital balances derived from the ledger (ARCHITECTURE §6.1-6.2).

Everything is recomputed from the journal on demand; there is no materialised cash
balance that could drift. ``today`` is the market-timezone date used to decide which
sell proceeds have settled (available from 00:00 America/New_York on the settlement
date; an unresolved ``settles_on`` is treated as unsettled).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.codec import dec_from_db, opt_date_from_db, str_from_db
from swing_trading_bot.ledger.db import Session


@dataclass(frozen=True, slots=True)
class Balances:
    portfolio_id: int
    today: date
    initial_capital: Decimal
    max_capital_allocation: Decimal
    cash: Decimal
    unsettled_proceeds: Decimal
    buy_commitments: Decimal
    cost_basis_exposure: Decimal
    deposits: Decimal
    dividends: Decimal
    fee_entries: Decimal
    adjustments: Decimal
    unresolved_settlements: int

    @property
    def settled_cash(self) -> Decimal:
        return self.cash - self.unsettled_proceeds

    @property
    def available_cash(self) -> Decimal:
        """Settled cash not committed to working buy orders."""
        return self.settled_cash - self.buy_commitments

    @property
    def deployed_capital(self) -> Decimal:
        """Cost basis of open positions plus active buy commitments (D8)."""
        return self.cost_basis_exposure + self.buy_commitments

    @property
    def capital_headroom(self) -> Decimal:
        return self.max_capital_allocation - self.deployed_capital

    @property
    def buying_capacity(self) -> Decimal:
        """min(settled available cash, capital headroom), never below zero."""
        return max(ZERO, min(self.available_cash, self.capital_headroom))


def compute_balances(s: Session, portfolio_id: int, today: date) -> Balances:
    row = s.fetch_one(
        "SELECT initial_capital, max_capital_allocation FROM portfolios WHERE id = ?",
        (portfolio_id,),
    )
    if row is None:
        raise LookupError(f"portfolio {portfolio_id} not found")
    initial, ceiling = dec_from_db(row[0]), dec_from_db(row[1])

    cash = unsettled = deposits = dividends = fees = adjustments = ZERO
    unresolved = 0
    for kind_raw, amount_raw, settles_raw in s.fetch_all(
        "SELECT kind, amount, settles_on FROM cash_entries WHERE portfolio_id = ?",
        (portfolio_id,),
    ):
        kind, amount, settles_on = (
            str_from_db(kind_raw),
            dec_from_db(amount_raw),
            opt_date_from_db(settles_raw),
        )
        cash += amount
        if settles_on is None:
            unresolved += 1
        if kind == "SELL" and (settles_on is None or settles_on > today):
            unsettled += amount
        elif kind == "DEPOSIT":
            deposits += amount
        elif kind == "DIVIDEND":
            dividends += amount
        elif kind == "FEE":
            fees += amount
        elif kind == "ADJUSTMENT":
            adjustments += amount

    commitments = sum(
        (
            dec_from_db(r[0])
            for r in s.fetch_all(
                "SELECT r.remaining FROM cash_reservations r JOIN orders o "
                "ON o.client_order_id = r.client_order_id "
                "WHERE r.status = 'ACTIVE' AND o.portfolio_id = ?",
                (portfolio_id,),
            )
        ),
        ZERO,
    )
    cost_basis = sum(
        (
            dec_from_db(r[1])
            for r in s.fetch_all(
                "SELECT qty, cost_basis FROM positions WHERE portfolio_id = ?", (portfolio_id,)
            )
            if dec_from_db(r[0]) > ZERO
        ),
        ZERO,
    )
    return Balances(
        portfolio_id=portfolio_id,
        today=today,
        initial_capital=initial,
        max_capital_allocation=ceiling,
        cash=cash,
        unsettled_proceeds=unsettled,
        buy_commitments=commitments,
        cost_basis_exposure=cost_basis,
        deposits=deposits,
        dividends=dividends,
        fee_entries=fees,
        adjustments=adjustments,
        unresolved_settlements=unresolved,
    )
