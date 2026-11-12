"""Read-only inspection views (``status``, ``portfolio``, ``orders``, ``halts``,
``audit``). Each runs inside ONE read transaction (``PRAGMA query_only``), never calls
the broker, never runs recovery, invariant halting or any other write.

Broker cash/equity is never shown: it is not the bot's capital (M1 acceptance #2).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.app.runtime import state_dict, unresolved_orders
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.baselines import BrokerBaselinesRepository
from swing_trading_bot.ledger.broker_outage import last_reset, load_streak
from swing_trading_bot.ledger.db import Database, Session
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import PortfolioSnapshot, VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    ReconciliationRunsRepository,
    ReservationsRepository,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.sync_keys import (
    BROKER_ACCOUNT_ID,
    BROKER_ENVIRONMENT,
    LAST_SUCCESSFUL_SYNC,
)
from swing_trading_bot.oms.rebaseline import cumulative_acknowledged


def _s(value: object) -> str | None:
    return None if value is None else str(value)


def _snapshot(db: Database, config: AppConfig, s: Session, now: datetime) -> PortfolioSnapshot:
    return VirtualPortfolio(db, config.portfolio()).snapshot(s, now=now)


def status_view(db: Database, config: AppConfig, now: datetime) -> dict[str, Any]:
    pf = VirtualPortfolio(db, config.portfolio())
    monitor = LossLimitMonitor(db, pf, config.loss_limits(db.identity.mode))
    with db.read() as s:
        integrity = integrity_report(s)
        state = TradingStateRepository().get(s)
        snap = pf.snapshot(s, now=now)
        loss = monitor.report(s, now=now)
        sync = SyncStateRepository()
        run = ReconciliationRunsRepository().latest(s)
        portfolio = pf.portfolio(s)
        baseline = BrokerBaselinesRepository().active(s, portfolio.id or 0)
        covered = complete_through(s)
        streak = load_streak(s)
        b = snap.balances
        settled = b.cash - b.unsettled_proceeds
        view: dict[str, Any] = {
            "ledger": str(db.path),
            "mode": db.identity.mode.value,
            "schema_version": integrity.schema_version,
            "integrity_ok": integrity.ok,
            "integrity_problems": integrity.problems(),
            "trading_state": state_dict(state),
            "execution_available": False if db.identity.mode is RuntimeMode.OBSERVE else None,
            "virtual_portfolio": {
                "initial_capital": str(portfolio.initial_capital),
                "max_capital_allocation": str(portfolio.max_capital_allocation),
                "account_profile": portfolio.account_profile,
                "cash": str(b.cash),
                "settled_cash": str(settled),
                "unsettled_proceeds": str(b.unsettled_proceeds),
                "buy_commitments": str(b.buy_commitments),
                "available_cash": str(settled - b.buy_commitments),
                "deployed_capital": str(b.cost_basis_exposure + b.buy_commitments),
                "capital_headroom": str(
                    b.max_capital_allocation - b.cost_basis_exposure - b.buy_commitments
                ),
                "realized_pnl": str(snap.realized_pnl),
                "unrealized_pnl": _s(snap.unrealized_pnl),
                "equity": _s(snap.equity),
                "market_exposure": _s(snap.market_exposure),
                "open_positions": sum(1 for p in snap.positions if p.qty),
                "entry_blockers": [e.value for e in snap.entry_blockers],
            },
            "loss_limits": {
                "blockers": [x.value for x in loss.blockers],
                "lockout_active": loss.lockout_active,
                "weekly_breach_active": loss.weekly_breach_active,
                "daily_baseline_id": None if loss.daily_baseline is None
                else loss.daily_baseline.id,
                "weekly_baseline_id": None if loss.weekly_baseline is None
                else loss.weekly_baseline.id,
                "problems": list(loss.problems),
            },
            "broker": {
                "recorded_account_id": sync.get(s, BROKER_ACCOUNT_ID),
                "recorded_environment": sync.get(s, BROKER_ENVIRONMENT),
                "last_successful_sync": sync.get(s, LAST_SUCCESSFUL_SYNC),
                "activities_complete_through": None if covered is None else covered.isoformat(),
                "active_baseline_id": None if baseline is None else baseline.id,
                "active_baseline_taken_at": None if baseline is None
                else baseline.taken_at.isoformat(),
                "acknowledged_unexplained_total": str(cumulative_acknowledged(s)),
                "outage_streak": None if streak is None else streak.to_dict(),
                "last_outage_reset": last_reset(s),
            },
            "latest_reconciliation": None if run is None else {
                "id": run.id, "result": run.result.value, "ts": run.ts.isoformat()},
            "unresolved_orders": unresolved_orders(s),
        }  # fmt: skip
    return view


def portfolio_view(db: Database, config: AppConfig, now: datetime) -> dict[str, Any]:
    with db.read() as s:
        snap = _snapshot(db, config, s, now)
    return {
        "ledger": str(db.path),
        "mode": db.identity.mode.value,
        "cash": str(snap.balances.cash),
        "equity": _s(snap.equity),
        "positions": [
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "cost_basis": str(p.cost_basis),
                "avg_cost": _s(p.avg_cost),
                "realized_pnl": str(p.realized_pnl),
                "mark": None if p.mark is None else str(p.mark.price),
                "mark_assessment": str(p.mark_assessment),
                "market_value": _s(p.market_value),
                "unrealized_pnl": _s(p.unrealized_pnl),
            }
            for p in snap.positions
        ],
    }


def orders_view(db: Database) -> dict[str, Any]:
    with db.read() as s:
        reservations = ReservationsRepository()
        rows = []
        for o in OrdersRepository().list_all(s):
            res = reservations.get(s, o.client_order_id)
            rows.append(
                {
                    "client_order_id": o.client_order_id,
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "status": o.status.value,
                    "qty": str(o.qty),
                    "filled_qty": str(o.filled_qty),
                    "limit_price": _s(o.limit_price),
                    "broker_order_id": o.broker_order_id,
                    "send_attempted_at": None
                    if o.send_attempted_at is None
                    else o.send_attempted_at.isoformat(),
                    "reservation": None
                    if res is None
                    else {"status": res.status.value, "remaining": str(res.remaining)},
                }
            )
    return {"ledger": str(db.path), "mode": db.identity.mode.value, "orders": rows}


def halts_view(db: Database) -> dict[str, Any]:
    with db.read() as s:
        repo = TradingStateRepository()
        state = repo.get(s)
        events = repo.events(s)
    return {
        "ledger": str(db.path),
        "trading_state": state_dict(state),
        "events": [
            {
                "ts": e.ts.isoformat(),
                "action": e.action,
                "reason_code": e.reason_code,
                "detail": e.detail,
                "actor": e.actor,
                "reconciliation_run_id": e.reconciliation_run_id,
            }
            for e in events
        ],
    }


def _rows(s: Session, sql: str, names: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    out = []
    for row in s.fetch_all(sql, (limit,)):
        item = dict(zip(names, row, strict=True))
        for key in ("detail_json", "diff_json"):
            if isinstance(item.get(key), str):
                item[key] = json.loads(item[key])
        out.append(item)
    return out


def audit_view(db: Database, *, limit: int = 20) -> dict[str, Any]:
    with db.read() as s:
        return {
            "ledger": str(db.path),
            "reconciliation_runs": _rows(
                s, "SELECT id, ts, result FROM reconciliation_runs ORDER BY id DESC LIMIT ?",
                ("id", "ts", "result"), limit),
            "rebaseline_requests": _rows(
                s, "SELECT id, created_at, actor, reason, status, unexplained_cash, "
                "acknowledged_amount FROM rebaseline_requests ORDER BY id DESC LIMIT ?",
                ("id", "created_at", "actor", "reason", "status", "unexplained_amount",
                 "acknowledged_amount"), limit),
            "rebaseline_events": _rows(
                s, "SELECT id, request_id, ts, action, actor FROM rebaseline_events "
                "ORDER BY id DESC LIMIT ?", ("id", "request_id", "ts", "action", "actor"), limit),
            "order_resolution_attempts": _rows(
                s, "SELECT id, client_order_id, ts, method, result FROM "
                "order_resolution_attempts ORDER BY id DESC LIMIT ?",
                ("id", "client_order_id", "ts", "method", "result"), limit),
            "calendar_fetches": _rows(
                s, "SELECT id, kind, range_start, range_end, fetched_at, source, status "
                "FROM calendar_fetches ORDER BY id DESC LIMIT ?",
                ("id", "kind", "start", "end", "fetched_at", "source", "status"), limit),
            "loss_limit_events": _rows(
                s, "SELECT id, ts, kind, applies_to, loss_pct FROM loss_limit_events "
                "ORDER BY id DESC LIMIT ?", ("id", "ts", "kind", "applies_to", "loss_pct"), limit),
        }  # fmt: skip
