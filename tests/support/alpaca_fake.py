"""Offline stand-ins for Alpaca: a scripted ReadOnlyHttp transport and payload builders.

Payloads are written as JSON text and decoded with the production ``decode_json`` (so
market-data numbers become Decimals exactly as in production). No network is used.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from swing_trading_bot.broker.alpaca_http import Service, decode_json
from swing_trading_bot.broker.errors import BrokerError

Handler = Callable[[Mapping[str, str]], object]


class FakeTransport:
    """Route (service, path) to scripted responses; record every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Service, str, dict[str, str]]] = []
        self._routes: dict[tuple[Service, str], deque[object | BrokerError | Handler]] = {}
        self._defaults: dict[tuple[Service, str], object | Handler] = {}

    def respond(self, service: Service, path: str, *responses: object) -> None:
        """Queue responses (JSON text, BrokerError to raise, or handler(params))."""
        self._routes.setdefault((service, path), deque()).extend(responses)

    def always(self, service: Service, path: str, response: object) -> None:
        self._defaults[(service, path)] = response

    def get_json(self, service: Service, path: str, params: Mapping[str, str]) -> object:
        self.calls.append((service, path, dict(params)))
        queue = self._routes.get((service, path))
        if queue:
            item = queue.popleft()
        elif (service, path) in self._defaults:
            item = self._defaults[(service, path)]
        else:
            raise AssertionError(f"unscripted request {service} {path}")
        if isinstance(item, BrokerError):
            raise item
        if callable(item):
            item = item(params)
        if isinstance(item, str):
            return decode_json(item)
        return item


def js(value: Any) -> str:
    return json.dumps(value)


#: Synthetic values in the field-name/JSON-type shape of the real paper ``/v2/account``
#: payload observed in Step 15B (names and types only; no real value is reproduced).
#: ``pattern_day_trader`` and ``daytrade_count`` are deliberately ABSENT, as observed.
ACCOUNT = {
    "id": "acct", "account_number": "PA0000", "status": "ACTIVE", "currency": "USD",
    "cash": "100000.12", "non_marginable_buying_power": "100000.12",
    "buying_power": "400000.48", "regt_buying_power": "200000.24",
    "effective_buying_power": "400000.48", "options_buying_power": "100000.12",
    "multiplier": "4", "trading_blocked": False, "account_blocked": False,
    "transfers_blocked": False, "trade_suspended_by_user": False, "shorting_enabled": True,
    "accrued_fees": "0", "pending_reg_taf_fees": "0.01", "equity": "100000.12",
    "last_equity": "100000.12", "portfolio_value": "100000.12",
    "long_market_value": "0", "short_market_value": "0", "position_market_value": "0",
    "initial_margin": "0", "maintenance_margin": "0", "last_maintenance_margin": "0",
    "sma": "0", "intraday_adjustments": "0", "balance_asof": "2026-11-06",
    "created_at": "2026-01-05T15:00:00.000000Z", "crypto_status": "INACTIVE",
    "crypto_tier": 0, "options_approved_level": 0, "options_trading_level": 0,
    "admin_configurations": {}, "user_configurations": None,
}  # fmt: skip

#: Synthetic values in the field-name/JSON-type shape of the real paper
#: ``/v2/account/configurations`` payload observed in Step 15B run #2 (8 fields; names and
#: types only, no real value reproduced). ``pdt_check`` and ``dtbp_check`` are ABSENT, as
#: observed.
CONFIG = {
    "closing_transactions_only": False, "disable_overnight_trading": False,
    "fractional_trading": False, "max_margin_multiplier": "1", "no_shorting": True,
    "ptp_no_exception_entry": False, "suspend_trade": False, "trade_confirm_email": "all",
}  # fmt: skip

CLOCK = {
    "timestamp": "2026-11-09T10:00:00.123456789-05:00", "is_open": True,
    "next_open": "2026-11-10T09:30:00-05:00", "next_close": "2026-11-09T16:00:00-05:00",
}  # fmt: skip


def order(
    coid: str, *, oid: str | None = None, status: str = "new", qty: str = "10",
    filled: str = "0", submitted: str = "2026-11-09T15:00:00.000001Z", **extra: Any,
) -> dict[str, Any]:  # fmt: skip
    base = {
        "id": oid or f"oid-{coid}", "client_order_id": coid, "symbol": "AAPL", "side": "buy",
        "type": "limit", "order_type": "limit", "time_in_force": "day", "qty": qty,
        "filled_qty": filled, "filled_avg_price": None, "limit_price": "150.25",
        "status": status, "created_at": submitted, "submitted_at": submitted,
        "updated_at": submitted, "extended_hours": False, "order_class": "",
    }  # fmt: skip
    base.update(extra)
    return base


def fill_activity(aid: str, *, order_id: str = "oid-1", qty: str = "4",
                  price: str = "150.2501", t: str = "2026-11-09T15:01:00.574Z",
                  **extra: Any) -> dict[str, Any]:  # fmt: skip
    base = {
        "id": aid, "activity_type": "FILL", "transaction_time": t, "type": "partial_fill",
        "price": price, "qty": qty, "side": "buy", "symbol": "AAPL", "leaves_qty": "6",
        "cum_qty": qty, "order_id": order_id, "order_status": "partially_filled",
    }  # fmt: skip
    base.update(extra)
    return base


def cash_activity(aid: str, kind: str, amount: str, *, day: str = "2026-11-09",
                  **extra: Any) -> dict[str, Any]:  # fmt: skip
    base = {"id": aid, "activity_type": kind, "date": day, "net_amount": amount,
            "description": kind, "status": "executed"}  # fmt: skip
    base.update(extra)
    return base


def now_fn(value: datetime) -> Callable[[], datetime]:
    return lambda: value


T_NOW = datetime(2026, 11, 9, 15, 5, tzinfo=UTC)
D = Decimal
