"""Helpers for loss-limit scenarios on simulation ledgers."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from swing_trading_bot.config.risk_limits import load_risk_limits
from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limit_store import CloseSource, SessionClose
from swing_trading_bot.ledger.loss_limits import LossLimitConfig, LossLimitMonitor, LossLimitReport
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from tests.support.portfolio import fill, finish, make_order, mark, reserve, seed_calendar

RISK_YAML = Path(__file__).resolve().parents[2] / "config" / "risk.yaml"
CONFIG = LossLimitConfig.from_risk_limits(
    load_risk_limits(RISK_YAML), calendar_max_age=timedelta(hours=36)
)
D = Decimal


def et(day: date, hh: int = 10, mm: int = 0) -> datetime:
    """A wall-clock time in America/New_York on ``day``, as UTC."""
    return datetime.combine(day, time(hh, mm), tzinfo=MARKET_TZ).astimezone(UTC)


def monitor(db: Database, pf: VirtualPortfolio) -> LossLimitMonitor:
    return LossLimitMonitor(db, pf, CONFIG)


def hold(db: Database, pf: VirtualPortfolio, symbol: str, qty: str, price: str, *,
         at: datetime) -> None:  # fmt: skip
    order = make_order(db, qty=qty, limit=price, symbol=symbol)
    reserve(pf, db, order)
    pf.process_fill(fill(order, qty, price, at=at), now=at)
    finish(pf, db, order, OrderStatus.FILLED)


def close(mon: LossLimitMonitor, symbol: str, day: date, price: str) -> None:
    mon.record_session_close(
        SessionClose(symbol, day, D(price), CloseSource.SESSION_CLOSE_BAR,
                     as_of=et(day, 16, 0), received_at=et(day, 16, 5))
    )  # fmt: skip


def evaluate_at(
    db: Database,
    pf: VirtualPortfolio,
    mon: LossLimitMonitor,
    now: datetime,
    prices: dict[str, str],
) -> LossLimitReport:
    """Refresh the calendar and marks at ``now`` (marks one minute old), then evaluate."""
    seed_calendar(db, now - timedelta(minutes=5))
    with db.transaction() as tx:
        for sym, price in prices.items():
            pf.record_mark(tx, mark(sym, price, as_of=now - timedelta(minutes=1), received=now))
    return mon.evaluate(now=now)
