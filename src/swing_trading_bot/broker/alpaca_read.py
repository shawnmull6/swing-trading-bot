"""Read-only Alpaca paper adapter (ARCHITECTURE §5.2, M1 step 9).

``AlpacaPaperReadAdapter`` implements :class:`BrokerReadPort` and
:class:`PriceMarkSource`. It is structurally read-only: it holds only a
:class:`ReadOnlyHttp` transport (whose single method issues HTTP GET), defines no
submit/replace/cancel method, and never constructs an alpaca-py trading client.

Endpoints (all ``GET``; trading host ``https://paper-api.alpaca.markets/v2``, market
data host ``https://data.alpaca.markets/v2``):

=========================  ==================================================
read                       endpoint
=========================  ==================================================
account                    ``/account``
account configuration      ``/account/configurations``
clock                      ``/clock``
calendar                   ``/calendar?start&end&date_type=TRADING|SETTLEMENT``
positions                  ``/positions``
orders (open / since)      ``/orders?status=open|all&direction=asc&limit=500
                           &nested=false&after&until``
order by client id         ``/orders:by_client_order_id?client_order_id=``
activities                 ``/account/activities?direction=asc&page_size
                           &page_token&after&until``
latest trades (marks)      data ``/stocks/trades/latest?symbols&feed``
session closes             data ``/stocks/bars?symbols&timeframe=1Day&start&end
                           &feed&adjustment=raw&limit``
=========================  ==================================================

Every response is parsed exactly (``broker.mapping``); malformed data raises
``InvalidBrokerDataError``. Activity ids are opaque; the cursor passed back is the last
id of the previous page as Alpaca documents (contract-test item).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Self

from swing_trading_bot.broker.alpaca_http import (
    DEFAULT_READ_POLICIES,
    AlpacaReadTransport,
    ReadOnlyHttp,
    ReadRetryPolicy,
    Service,
    Sleeper,
    real_sleep,
    with_read_retries,
)
from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    OrderNotFoundError,
    UnsupportedBrokerOperationError,
)
from swing_trading_bot.broker.mapping import (
    account_config_from_raw,
    account_from_raw,
    activity_from_raw,
    clock_from_raw,
    json_number,
    opt_decimal,
    order_from_raw,
    position_from_raw,
    session_from_raw,
    settlement_days_from_raw,
    text,
    timestamp,
)
from swing_trading_bot.broker.models import (
    AccountConfigSnapshot,
    AccountSnapshot,
    ActivityPage,
    DailyBarClose,
    MarketClock,
)
from swing_trading_bot.broker.ports import MAX_ACTIVITY_PAGE_SIZE
from swing_trading_bot.config.settings import Settings
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarFetch, CalendarKind
from swing_trading_bot.domain.enums import MarkSource
from swing_trading_bot.domain.models import BrokerOrderSnapshot, BrokerPosition, PriceMark

ORDERS_PAGE_LIMIT: Final = 500
MAX_ORDER_PAGES: Final = 50
MAX_BAR_PAGES: Final = 20
FEEDS: Final = frozenset({"iex", "sip", "delayed_sip"})
SOURCE: Final = "alpaca"
ENVIRONMENT: Final = "paper"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_param(value: datetime) -> str:
    """RFC 3339 UTC for query parameters; naive datetimes are rejected (the SDK would
    silently assume UTC)."""
    if value.utcoffset() is None:
        raise ValueError("query timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_list(payload: object, what: str) -> list[Mapping[str, Any]]:
    if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
        raise InvalidBrokerDataError(f"{what}: expected a JSON array of objects")
    return payload


def _as_object(payload: object, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidBrokerDataError(f"{what}: expected a JSON object")
    return payload


class AlpacaPaperReadAdapter:
    """Implements BrokerReadPort and PriceMarkSource against Alpaca paper. Read-only."""

    def __init__(
        self,
        transport: ReadOnlyHttp,
        *,
        clock: Callable[[], datetime] = _utc_now,
        feed: str = "iex",
        sleep: Sleeper = real_sleep,
        policies: Mapping[str, ReadRetryPolicy] = DEFAULT_READ_POLICIES,
    ) -> None:
        if feed not in FEEDS:
            raise ValueError(f"unsupported market-data feed {feed!r}")
        self._http = transport
        self._clock = clock
        self._feed = feed
        self._sleep = sleep
        self._policies = dict(policies)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = _utc_now,
        feed: str = "iex",
        sleep: Sleeper = real_sleep,
    ) -> Self:
        """The production constructor: paper-only settings, GET-only transport."""
        return cls(AlpacaReadTransport(settings), clock=clock, feed=feed, sleep=sleep)

    def __repr__(self) -> str:
        return f"AlpacaPaperReadAdapter(feed={self._feed!r}, read-only)"

    def _get(self, op: str, service: Service, path: str, params: Mapping[str, str]) -> object:
        return with_read_retries(
            lambda: self._http.get_json(service, path, params), self._policies[op], self._sleep
        )

    # ------------------------------------------------------------- account
    def get_account(self) -> AccountSnapshot:
        raw = _as_object(self._get("account", "trading", "/account", {}), "account")
        return account_from_raw(raw, as_of=self._clock(), environment=ENVIRONMENT)

    def get_account_config(self) -> AccountConfigSnapshot:
        raw = self._get("account_config", "trading", "/account/configurations", {})
        return account_config_from_raw(_as_object(raw, "account configuration"))

    def get_clock(self) -> MarketClock:
        return clock_from_raw(_as_object(self._get("clock", "trading", "/clock", {}), "clock"))

    def get_calendar(self, kind: CalendarKind, start: date, end: date) -> CalendarFetch:
        if start > end:
            raise ValueError("start must not be after end")
        rows = _as_list(
            self._get("calendar", "trading", "/calendar", {
                "start": start.isoformat(), "end": end.isoformat(), "date_type": kind.value,
            }),
            "calendar",
        )  # fmt: skip
        fetched_at = self._clock()
        if kind is CalendarKind.TRADING:
            sessions = tuple(session_from_raw(r) for r in rows)
            return CalendarFetch(kind, start, end, fetched_at, SOURCE, sessions=sessions)
        # SETTLEMENT rows are trade-date rows selected by settlement_date (§12.26): the
        # settlement days are their settlement_date values, never the trade dates.
        days = settlement_days_from_raw(rows)
        return CalendarFetch(kind, start, end, fetched_at, SOURCE, settlement_days=days)

    # ----------------------------------------------------------- positions
    def get_positions(self) -> list[BrokerPosition]:
        rows = _as_list(self._get("positions", "trading", "/positions", {}), "positions")
        positions = [position_from_raw(r) for r in rows]
        symbols = [p.symbol for p in positions]
        if len(set(symbols)) != len(symbols):
            raise InvalidBrokerDataError("positions: duplicate symbol")
        return positions

    def get_position_price_crosscheck(self) -> dict[str, Decimal | None]:
        """``Position.current_price`` per symbol. It has **no timestamp**, so it is a
        cross-check only and must never be used as a price mark (U7, §3 V10)."""
        rows = _as_list(self._get("positions", "trading", "/positions", {}), "positions")
        return {text(r, "symbol"): opt_decimal(r, "current_price") for r in rows}

    # -------------------------------------------------------------- orders
    def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        return self._orders("open", after=None, until=None)

    def get_orders(
        self, *, after: datetime, until: datetime | None = None
    ) -> list[BrokerOrderSnapshot]:
        return self._orders("all", after=after, until=until)

    def _orders(
        self, status: str, *, after: datetime | None, until: datetime | None
    ) -> list[BrokerOrderSnapshot]:
        """Exhaustive order retrieval, or an error — never a silently truncated list.

        ``GET /v2/orders`` has no page token: the only way to page is by submission time
        (``direction=asc``, ``limit<=500``, ``after`` exclusive). Each next request starts
        1 µs before the last page's newest ``submitted_at`` (so orders sharing that
        timestamp are re-read) and results are deduplicated by order id.

        Completeness is impossible when a full page contains only one timestamp: 500+
        orders share it and the API returns an unspecified 500 of them. That, a response
        not sorted by submission time, a missing ``submitted_at``, a non-advancing cursor
        or the page cap all raise :class:`IncompleteRetrievalError` (fail closed).
        """
        collected: dict[str, BrokerOrderSnapshot] = {}
        cursor = after
        for _ in range(MAX_ORDER_PAGES):
            params = {"status": status, "direction": "asc", "nested": "false",
                      "limit": str(ORDERS_PAGE_LIMIT)}  # fmt: skip
            if cursor is not None:
                params["after"] = iso_param(cursor)
            if until is not None:
                params["until"] = iso_param(until)
            rows = _as_list(self._get("orders", "trading", "/orders", params), "orders")
            if len(rows) > ORDERS_PAGE_LIMIT:
                raise InvalidBrokerDataError("orders: page larger than the requested limit")
            page = [order_from_raw(r) for r in rows]
            stamps: list[datetime] = []
            for o in page:
                if o.submitted_at is None:
                    raise IncompleteRetrievalError(
                        f"order {o.broker_order_id} has no submitted_at: cannot page safely",
                        partial=tuple(collected.values()),
                    )
                stamps.append(o.submitted_at)
                existing = collected.get(o.broker_order_id)
                if existing is not None and existing.client_order_id != o.client_order_id:
                    raise InvalidBrokerDataError(f"order id {o.broker_order_id} changed identity")
            if stamps != sorted(stamps):
                raise IncompleteRetrievalError(
                    "orders: response not sorted by submitted_at; paging would be unsafe",
                    partial=tuple(collected.values()),
                )
            new = [o for o in page if o.broker_order_id not in collected]
            collected.update((o.broker_order_id, o) for o in page)
            if len(rows) < ORDERS_PAGE_LIMIT:
                return list(collected.values())  # short page: provably the tail
            if stamps[0] == stamps[-1]:
                raise IncompleteRetrievalError(
                    f"orders: {ORDERS_PAGE_LIMIT}+ orders share submitted_at {stamps[0]}; "
                    "the endpoint cannot enumerate them exhaustively",
                    partial=tuple(collected.values()),
                )
            if not new:
                raise IncompleteRetrievalError(
                    "orders: pagination cursor did not advance", partial=tuple(collected.values())
                )
            cursor = stamps[-1] - timedelta(microseconds=1)
        raise IncompleteRetrievalError(
            f"orders exceeded {MAX_ORDER_PAGES} pages", partial=tuple(collected.values())
        )

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        """None on 404 — which is NOT proof that the order does not exist (§10.3)."""
        if not client_order_id:
            raise ValueError("client_order_id is required")
        try:
            raw = self._get("order_by_client_id", "trading", "/orders:by_client_order_id",
                            {"client_order_id": client_order_id})  # fmt: skip
        except OrderNotFoundError:
            return None
        order = order_from_raw(_as_object(raw, "order"))
        if order.client_order_id != client_order_id:
            raise InvalidBrokerDataError("lookup returned a different client_order_id")
        return order

    # ---------------------------------------------------------- activities
    def get_activities(
        self,
        *,
        page_token: str | None,
        page_size: int = MAX_ACTIVITY_PAGE_SIZE,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> ActivityPage:
        if not 1 <= page_size <= MAX_ACTIVITY_PAGE_SIZE:
            raise ValueError(f"page_size must be within 1..{MAX_ACTIVITY_PAGE_SIZE}")
        params = {"direction": "asc", "page_size": str(page_size)}
        if page_token is not None:
            params["page_token"] = page_token
        if after is not None:
            params["after"] = iso_param(after)
        if until is not None:
            params["until"] = iso_param(until)
        rows = _as_list(
            self._get("activities", "trading", "/account/activities", params), "activities"
        )
        if len(rows) > page_size:
            raise InvalidBrokerDataError("activities: page larger than requested")
        activities = tuple(activity_from_raw(r) for r in rows)
        ids = [a.activity_id for a in activities]
        if len(set(ids)) != len(ids):
            raise InvalidBrokerDataError("activities: duplicate id within one page")
        if page_token is not None and page_token in ids:
            raise InvalidBrokerDataError("activities: page repeats the cursor activity")
        return ActivityPage(activities=activities, page_size=page_size)

    # ------------------------------------------------------- market data
    def get_marks(self, symbols: Sequence[str]) -> Mapping[str, PriceMark]:
        """Latest trade per symbol with its exchange timestamp. Symbols the feed does not
        return are absent (missing marks are never defaulted). Freshness is judged by
        ``domain.valuation.assess_mark``, not here."""
        wanted = self._symbols(symbols)
        if not wanted:
            return {}
        payload = _as_object(
            self._get("marks", "data", "/stocks/trades/latest",
                      {"symbols": ",".join(wanted), "feed": self._feed}),
            "latest trades",
        )  # fmt: skip
        trades = _as_object(payload.get("trades"), "latest trades.trades")
        received = self._clock()
        marks: dict[str, PriceMark] = {}
        for symbol, raw in trades.items():
            if symbol not in wanted:
                raise InvalidBrokerDataError(f"latest trades: unrequested symbol {symbol!r}")
            trade = _as_object(raw, f"latest trade {symbol}")
            price = json_number(trade, "p")
            if price <= 0:
                raise InvalidBrokerDataError(f"latest trade {symbol}: non-positive price")
            marks[symbol] = PriceMark(
                symbol=symbol, price=price, as_of=timestamp(trade, "t"), received_at=received,
                source=MarkSource.LATEST_TRADE, feed=self._feed,
            )  # fmt: skip
        return marks

    def get_official_closes(
        self, symbols: Sequence[str], session_date: date
    ) -> dict[str, DailyBarClose]:
        """Official consolidated closing prices: NOT SUPPORTED. No verified official-close
        source is integrated (required before M2 execution). Fails closed rather than
        substituting daily-bar observations."""
        del symbols, session_date
        raise UnsupportedBrokerOperationError(
            "no verified official-close source; daily-bar closes are observations only"
        )

    def get_daily_bar_closes(
        self, symbols: Sequence[str], session_date: date
    ) -> dict[str, DailyBarClose]:
        """Daily-bar close observations for ``session_date`` (missing symbols are absent).
        Each carries its feed; for IEX this is IEX's last trade, not an official close."""
        wanted = self._symbols(symbols)
        closes: dict[str, DailyBarClose] = {}
        if not wanted:
            return closes
        token: str | None = None
        for _ in range(MAX_BAR_PAGES):
            params = {"symbols": ",".join(wanted), "timeframe": "1Day",
                      "start": session_date.isoformat(), "end": session_date.isoformat(),
                      "feed": self._feed, "adjustment": "raw", "limit": "1000"}  # fmt: skip
            if token is not None:
                params["page_token"] = token
            payload = _as_object(self._get("session_closes", "data", "/stocks/bars", params),
                                 "bars")  # fmt: skip
            received = self._clock()
            bars = _as_object(payload.get("bars") or {}, "bars.bars")
            for symbol, rows in bars.items():
                if symbol not in wanted:
                    raise InvalidBrokerDataError(f"bars: unrequested symbol {symbol!r}")
                for bar in _as_list(rows, f"bars {symbol}"):
                    as_of = timestamp(bar, "t")
                    if as_of.astimezone(MARKET_TZ).date() != session_date:
                        continue
                    if symbol in closes:
                        raise InvalidBrokerDataError(f"bars: two daily bars for {symbol}")
                    closes[symbol] = DailyBarClose(
                        symbol, session_date, json_number(bar, "c"), as_of, received, self._feed
                    )
            next_token = payload.get("next_page_token")
            if next_token is None:
                return closes
            if not isinstance(next_token, str) or next_token == token:
                raise InvalidBrokerDataError("bars: invalid next_page_token")
            token = next_token
        raise InvalidBrokerDataError(f"bars exceeded {MAX_BAR_PAGES} pages")

    @staticmethod
    def _symbols(symbols: Iterable[str]) -> list[str]:
        out = list(dict.fromkeys(symbols))
        for s in out:
            if not s or not s.replace(".", "").isalnum() or s.upper() != s:
                raise ValueError(f"invalid symbol {s!r}")
        return out
