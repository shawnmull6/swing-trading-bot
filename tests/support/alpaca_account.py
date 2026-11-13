"""A stateful, offline Alpaca paper account behind the REAL ``AlpacaPaperReadAdapter``.

Only the HTTP transport is fake (``FakeTransport``: GET-only, no sockets). The account
holds cash, positions, open orders, posted activities and prices, and answers the
read endpoints the adapter uses from that state, so the CLI runs end to end offline.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import BrokerError, OrderNotFoundError
from swing_trading_bot.domain.calendar import MARKET_TZ, MAX_SETTLEMENT_LAG_DAYS, TradingSession
from tests.support.alpaca_fake import ACCOUNT, CONFIG, FakeTransport
from tests.support.calendar_fixture import fixture_sessions

# Monday 2026-11-09 10:00 ET, inside a regular session of the fixture calendar.
START = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
ACCOUNT_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"  # synthetic UUID, not a real account
READ_PATHS = frozenset(
    {
        "/account",
        "/account/configurations",
        "/clock",
        "/calendar",
        "/positions",
        "/orders",
        "/orders:by_client_order_id",
        "/account/activities",
        "/stocks/trades/latest",
        "/stocks/bars",
    }
)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def _parse(params: Mapping[str, str], key: str) -> datetime | None:
    raw = params.get(key)
    return None if raw is None else datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _calendar_row(s: TradingSession) -> dict[str, Any]:
    return {
        "date": s.session_date.isoformat(),
        "open": s.open_at.astimezone(MARKET_TZ).strftime("%H:%M"),
        "close": s.close_at.astimezone(MARKET_TZ).strftime("%H:%M"),
        "session_open": "0400",  # synthetic; extended-hours fields are not mapped
        "session_close": "2000",
        "settlement_date": None if s.settlement_date is None else s.settlement_date.isoformat(),
    }


@dataclass
class FakeAlpacaAccount:
    clock: Clock = field(default_factory=Clock)
    account_id: str = ACCOUNT_ID
    cash: Decimal = Decimal("100000.00")
    account_overrides: dict[str, Any] = field(default_factory=dict)
    config_overrides: dict[str, Any] = field(default_factory=dict)
    """Merged over ``CONFIG`` for ``/account/configurations`` (a ``None`` value = JSON null)."""
    positions: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    activities: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    prices: dict[str, Decimal] = field(default_factory=dict)
    failures: dict[str, list[BrokerError]] = field(default_factory=dict)
    down: dict[str, Callable[[], BrokerError]] = field(default_factory=dict)
    """Paths that fail on every request until removed ("*" = every path)."""
    transport: FakeTransport = field(default_factory=FakeTransport)
    _seq: int = 0

    def __post_init__(self) -> None:
        routes = {
            "/account": self._account,
            "/account/configurations": lambda _p: {**CONFIG, **self.config_overrides},
            "/clock": self._clock,
            "/calendar": self._calendar,
            "/positions": lambda _p: list(self.positions),
            "/orders": self._orders,
            "/orders:by_client_order_id": self._by_client_id,
            "/account/activities": self._activities,
        }
        for path, handler in routes.items():
            self.transport.always("trading", path, self._guard(path, handler))
        self.transport.always(
            "data", "/stocks/trades/latest", self._guard("/stocks/trades/latest", self._trades)
        )
        self.transport.always("data", "/stocks/bars", self._guard("/stocks/bars", self._bars))

    # ---------------------------------------------------------------- control
    def adapter(self) -> AlpacaPaperReadAdapter:
        return AlpacaPaperReadAdapter(self.transport, clock=self.clock, sleep=lambda _d: None)

    def fail(self, path: str, *errors: BrokerError) -> None:
        self.failures.setdefault(path, []).extend(errors)

    def deposit(self, amount: str, kind: str = "CSD") -> str:
        """An external cash movement posted now (not the bot's)."""
        self.cash += Decimal(amount)
        return self.post(
            {"activity_type": kind, "date": self.clock.now.astimezone(MARKET_TZ).date().isoformat(),
             "net_amount": amount, "description": kind, "status": "executed"}
        )  # fmt: skip

    def post(self, row: dict[str, Any]) -> str:
        self._seq += 1
        aid = f"{self.clock.now:%Y%m%d%H%M%S%f}::{self._seq:08d}"
        self.activities.append((self.clock.now, {"id": aid, **row}))
        return aid

    def paths_called(self) -> set[str]:
        return {path for _service, path, _params in self.transport.calls}

    # ---------------------------------------------------------------- handlers
    def _guard(self, path: str, handler: Any) -> Any:
        def run(params: Mapping[str, str]) -> object:
            queue = self.failures.get(path)
            if queue:
                raise queue.pop(0)
            outage = self.down.get(path) or self.down.get("*")
            if outage is not None:
                raise outage()
            return json.dumps(handler(params))

        return run

    def _account(self, _p: Mapping[str, str]) -> dict[str, Any]:
        return {
            **ACCOUNT,
            "id": self.account_id,
            "cash": str(self.cash),
            "non_marginable_buying_power": str(self.cash),
            "equity": str(self.cash),
            "pending_reg_taf_fees": "0",
            **self.account_overrides,
        }

    def _clock(self, _p: Mapping[str, str]) -> dict[str, Any]:
        local = self.clock.now.astimezone(MARKET_TZ)
        day = local.date()
        return {
            "timestamp": local.isoformat(),
            "is_open": 9 * 60 + 30 <= local.hour * 60 + local.minute < 16 * 60
            and day.weekday() < 5,
            "next_open": datetime.combine(day + timedelta(days=1), datetime.min.time(),
                                          tzinfo=MARKET_TZ).replace(hour=9, minute=30).isoformat(),
            "next_close": local.replace(hour=16, minute=0, second=0, microsecond=0).isoformat(),
        }  # fmt: skip

    def _calendar(self, p: Mapping[str, str]) -> list[dict[str, Any]]:
        """The observed Alpaca shape (Step 15B live run #3): TRADING returns one row per
        session in range; SETTLEMENT returns the same trade-date rows, selected by their
        ``settlement_date`` falling in range (so early rows predate ``start`` and several
        rows can share one settlement date). Values are synthetic fixture data."""
        start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        if p["date_type"] == "SETTLEMENT":
            lookback = start - timedelta(days=MAX_SETTLEMENT_LAG_DAYS)
            return [
                _calendar_row(s)
                for s in fixture_sessions(lookback, end)
                if s.settlement_date is not None and start <= s.settlement_date <= end
            ]
        return [_calendar_row(s) for s in fixture_sessions(start, end)]

    def _orders(self, p: Mapping[str, str]) -> list[dict[str, Any]]:
        return list(self.open_orders) if p["status"] in {"open", "all"} else []

    def _by_client_id(self, p: Mapping[str, str]) -> dict[str, Any]:
        coid = p["client_order_id"]
        for o in self.open_orders:
            if o["client_order_id"] == coid:
                return o
        raise OrderNotFoundError("not found", status_code=404)

    def _activities(self, p: Mapping[str, str]) -> list[dict[str, Any]]:
        after, until = _parse(p, "after"), _parse(p, "until")
        token, size = p.get("page_token"), int(p["page_size"])
        rows = [
            row
            for posted, row in sorted(self.activities, key=lambda x: x[1]["id"])
            if (after is None or posted > after)
            and (until is None or posted <= until)
            and (token is None or row["id"] > token)
        ]
        return rows[:size]

    def _bars(self, p: Mapping[str, str]) -> dict[str, Any]:
        day = date.fromisoformat(p["start"])
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=MARKET_TZ)
        t = midnight.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return {
            "bars": {
                s: [
                    {
                        "t": t,
                        "o": float(px),
                        "h": float(px),
                        "l": float(px),
                        "c": float(px),
                        "v": 1000,
                    }
                ]
                for s in p["symbols"].split(",")
                if (px := self.prices.get(s)) is not None
            },
            "next_page_token": None,
        }

    def _trades(self, p: Mapping[str, str]) -> dict[str, Any]:
        wanted = p["symbols"].split(",")
        return {
            "trades": {
                s: {"p": float(self.prices[s]), "t": self.clock.now.isoformat()}
                for s in wanted
                if s in self.prices
            }
        }


class AccountSession:
    """A ``requests.Session`` stand-in serving a :class:`FakeAlpacaAccount` over the REAL
    ``AlpacaReadTransport`` (so HTTP status mapping, headers and host checks run). It has
    no write methods; every request is recorded without its headers' values."""

    def __init__(self, account: FakeAlpacaAccount) -> None:
        self.account = account
        self.requests: list[tuple[str, str]] = []
        self.header_names: set[str] = set()
        self.trust_env = True

    def get(self, url: str, *, params: Mapping[str, str], headers: Mapping[str, str],
            timeout: object, allow_redirects: bool) -> Any:  # fmt: skip
        import requests  # noqa: PLC0415

        from swing_trading_bot.broker.errors import (  # noqa: PLC0415
            AuthenticationError,
            RateLimitedError,
            ServiceUnavailableError,
        )

        assert allow_redirects is False and timeout
        self.requests.append(("GET", url))
        self.header_names.update(headers)
        service = "data" if url.startswith("https://data.alpaca.markets/") else "trading"
        path = url.split("/v2", 1)[1]
        response = requests.Response()
        response.url = url
        try:
            route: tuple[Any, str] = (service, path)
            handler = self.account.transport._defaults[route]
            body = handler(params) if callable(handler) else handler
            response.status_code, response._content = 200, str(body).encode()
        except AuthenticationError:
            response.status_code, response._content = 401, b'{"message":"unauthorized"}'
        except OrderNotFoundError:
            response.status_code, response._content = 404, b'{"message":"not found"}'
        except RateLimitedError:
            response.status_code, response._content = 429, b'{"message":"rate limit"}'
        except ServiceUnavailableError as exc:
            if exc.kind == "timeout":
                raise requests.Timeout("timed out") from None
            response.status_code, response._content = 503, b'{"message":"unavailable"}'
        return response
