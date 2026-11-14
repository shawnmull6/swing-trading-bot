"""GET-only HTTP transport for Alpaca reads, and bounded read retries (M1 step 9).

Why the adapter does not call alpaca-py's client methods (alpaca-py 0.45.0, inspected):

1. ``TradingClient`` has no account-activities method (ARCHITECTURE §3 V3).
2. ``RESTClient._one_request`` decodes bodies with ``response.json()``, turning JSON
   numbers into floats. Market-data endpoints return prices as JSON numbers, so exact
   values would be lost. This transport decodes with ``parse_float=Decimal``.
3. ``RESTClient._request`` silently retries HTTP 429 and 504 for **every** verb
   (``DEFAULT_RETRY_EXCEPTION_CODES = [429, 504]``, 3 attempts, 3 s sleeps), and
   ``retry_attempts=0`` is ignored. Retries here are explicit, bounded and per
   operation, and apply to reads only.
4. ``RESTClient`` passes no request timeout, so a stalled connection can hang forever.
5. ``to_request_fields`` treats naive datetimes as UTC; this transport accepts only
   pre-formatted string parameters built from timezone-aware values.

alpaca-py is still the source of the base URLs (``BaseURL``) and of the authentication
headers (``RESTClient._get_default_headers``, called once at construction on a
throw-away client that is never stored). The transport exposes exactly one method,
:meth:`AlpacaReadTransport.get_json`, which can only issue HTTP GET to the paper
trading host or the market-data host. It cannot construct a write request.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Final, Literal, Protocol

import requests
from alpaca.common.enums import BaseURL
from alpaca.common.rest import RESTClient

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    AuthFailureKind,
    BrokerError,
    BrokerRequestError,
    DataEntitlementKind,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.mapping import check_payload_depth
from swing_trading_bot.config.settings import Settings

Service = Literal["trading", "data"]
API_VERSION: Final = "v2"
BASE_URLS: Final[Mapping[Service, str]] = {
    "trading": BaseURL.TRADING_PAPER.value,
    "data": BaseURL.DATA.value,
}
ALLOWED_ORIGINS: Final[Mapping[Service, str]] = {
    "trading": "https://paper-api.alpaca.markets",
    "data": "https://data.alpaca.markets",
}
"""The exact, HTTPS-only hosts this transport may contact. The SDK's live trading URL is
never among them; a change in the SDK constants is refused rather than followed."""
CONNECT_TIMEOUT_S: Final = 5
READ_TIMEOUT_S: Final = 20
_MAX_ERROR_TEXT: Final = 300
MAX_FORBIDDEN_BODY_BYTES: Final = 2048
"""A 403 body longer than this is not inspected (classified unresolved)."""
KNOWN_ENTITLEMENT_MESSAGES: Final[Mapping[str, tuple[DataEntitlementKind, str]]] = {
    "subscription does not permit querying recent sip data": (
        DataEntitlementKind.RECENT_DATA_WINDOW,
        "sip",
    ),
}
"""Normalized ``message`` values of a data-host 403 known to name a subscription limit,
mapped to (kind, feed). Only the publicly reported recent-SIP wording is listed (Step
16.5A); Step 16.5 did not capture the body, so even it is not live-verified. No pattern
matching: any other message stays an unresolved AuthenticationError. Add an entry only
from captured live evidence."""


class ReadOnlyHttp(Protocol):
    def get_json(self, service: Service, path: str, params: Mapping[str, str]) -> object: ...


def verify_allowlist() -> None:
    """Refuse to run if the SDK's base URLs no longer equal the HTTPS allowlist."""
    for service, origin in ALLOWED_ORIGINS.items():
        if not origin.startswith("https://") or BASE_URLS[service] != origin:
            raise ValueError(f"Alpaca {service} base URL is not the allowlisted paper origin")
    if any(o == BaseURL.TRADING_LIVE.value for o in ALLOWED_ORIGINS.values()):
        raise ValueError("the live trading host must never be allowlisted")


def _check_path(path: str) -> None:
    if not path.startswith("/") or "://" in path or ".." in path or "?" in path:
        raise ValueError(f"invalid API path {path!r}")


def decode_json(text: str) -> object:
    """Decode with exact decimals: JSON numbers with a fraction/exponent -> Decimal.

    Every broker response passes here. Nesting deeper than
    :data:`~swing_trading_bot.broker.mapping.MAX_PAYLOAD_DEPTH` — including nesting deep
    enough to make the decoder itself raise ``RecursionError`` — is a malformed payload
    (:class:`InvalidBrokerDataError`, Step 15B.6B), never an internal error."""
    try:
        doc = json.loads(text, parse_float=Decimal)
    except ValueError:
        raise InvalidBrokerDataError("response body is not valid JSON") from None
    except RecursionError:
        raise InvalidBrokerDataError("response body nesting is too deep") from None
    check_payload_depth(doc)
    return doc


def _retry_after(response: requests.Response) -> timedelta | None:
    value = response.headers.get("Retry-After")
    if value is None or not value.strip().isdigit():
        return None
    return timedelta(seconds=int(value.strip()))


def _error_text(response: requests.Response) -> str:
    return response.text[:_MAX_ERROR_TEXT].replace("\n", " ")


def _forbidden_message(response: requests.Response) -> tuple[str | None, str]:
    """(normalized ``message``, evidence label) from a 403 body, bounded and fail-closed:
    the body is inspected only if it is at most :data:`MAX_FORBIDDEN_BODY_BYTES`, UTF-8,
    a JSON object with a string ``message``; anything else is an evidence label only.
    The raw body is never returned, logged or attached to an error."""
    raw = response.content or b""
    if not raw.strip():
        return None, "no_body"
    if len(raw) > MAX_FORBIDDEN_BODY_BYTES:
        return None, "oversized_body"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "malformed_body"
    message = doc.get("message") if isinstance(doc, dict) else None
    if not isinstance(message, str):
        return None, "no_message"
    return " ".join(message.split()).lower().rstrip("."), "unrecognized_message"


def classify_forbidden(
    response: requests.Response, context: str, service: Service | None
) -> BrokerError:
    """The error for an HTTP 403 (Step 16.5A).

    Only a data-host 403 whose normalized body ``message`` is listed in
    :data:`KNOWN_ENTITLEMENT_MESSAGES` becomes a
    :class:`MarketDataEntitlementError` (blocks the data operation; says nothing about
    the trading credentials). Every other 403 — trading host, no/oversized/malformed
    body, unrecognized message — stays an :class:`AuthenticationError` of kind
    ``FORBIDDEN_UNRESOLVED`` (fail closed, AUTH halt as before). Error messages carry
    only fixed text, the listed feed and an evidence label; never the body."""
    if service != "data":
        return AuthenticationError(f"{context}: HTTP 403 (unresolved: trading host)",
                                   status_code=403, evidence="trading_host")  # fmt: skip
    message, evidence = _forbidden_message(response)
    known = KNOWN_ENTITLEMENT_MESSAGES.get(message) if message is not None else None
    if known is None:
        return AuthenticationError(f"{context}: HTTP 403 (unresolved: {evidence})",
                                   status_code=403, evidence=evidence)  # fmt: skip
    kind, feed = known
    return MarketDataEntitlementError(
        f"{context}: HTTP 403: market data not permitted ({kind}, feed {feed})",
        kind=kind, feed=feed,
    )  # fmt: skip


def raise_for_status(
    response: requests.Response, context: str, service: Service | None = None
) -> None:
    """Map a non-2xx response to the error taxonomy. ``service`` is the host the request
    went to; 403 classification depends on it (only the data host can produce a
    market-data entitlement error)."""
    status = response.status_code
    if 200 <= status < 300:
        return
    detail = f"{context}: HTTP {status}: {_error_text(response)}"
    if status == 401:
        raise AuthenticationError(f"{context}: HTTP 401", status_code=401,
                                  kind=AuthFailureKind.CREDENTIALS_REJECTED)  # fmt: skip
    if status == 403:
        raise classify_forbidden(response, context, service)
    if status == 404:
        raise OrderNotFoundError(detail, status_code=status)
    if status == 429:
        raise RateLimitedError(detail, retry_after=_retry_after(response), status_code=status)
    if status >= 500:
        raise ServiceUnavailableError(detail, status_code=status)
    if 300 <= status < 400:
        raise InvalidBrokerDataError(f"{context}: unexpected redirect (HTTP {status})")
    raise BrokerRequestError(detail, status_code=status)


class AlpacaReadTransport:
    """HTTP GET to Alpaca paper trading / market data. Nothing else is possible."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        connect_timeout_s: int = CONNECT_TIMEOUT_S,
        read_timeout_s: int = READ_TIMEOUT_S,
    ) -> None:
        if settings.trading_mode != "paper":  # pragma: no cover - Settings enforces it
            raise ValueError("Alpaca transport is paper-only")
        verify_allowlist()
        api_key, secret = settings.secret_values()
        # Header construction from the SDK; the client object is not kept.
        self._headers: dict[str, str] = dict(
            RESTClient(
                base_url=BASE_URLS["trading"], api_key=api_key, secret_key=secret,
                api_version=API_VERSION,
            )._get_default_headers()
        )  # fmt: skip
        self._session = session if session is not None else requests.Session()
        # Never pick up proxies, CA bundles or .netrc credentials from the environment.
        self._session.trust_env = False
        self._timeout = (connect_timeout_s, read_timeout_s)

    def __repr__(self) -> str:
        return "AlpacaReadTransport(paper, GET-only)"

    def get_json(self, service: Service, path: str, params: Mapping[str, str]) -> object:
        _check_path(path)
        base = ALLOWED_ORIGINS[service]
        url = f"{base}/{API_VERSION}{path}"
        context = f"GET {service}{path}"
        try:
            response = self._session.get(
                url,
                params=dict(params),
                headers=self._headers,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise ServiceUnavailableError(f"{context}: timeout", kind="timeout") from None
        except requests.RequestException as exc:
            raise ServiceUnavailableError(
                f"{context}: {type(exc).__name__}", kind="network"
            ) from None
        final = getattr(response, "url", None)
        if final and not str(final).startswith(base + "/"):
            raise InvalidBrokerDataError(f"{context}: response came from an unexpected host")
        raise_for_status(response, context, service)
        return decode_json(response.text)


# --------------------------------------------------------------- read retries


@dataclass(frozen=True, slots=True)
class ReadRetryPolicy:
    """Bounded retries for one read operation. Only RateLimitedError and
    ServiceUnavailableError are retried; everything else is raised at once."""

    max_attempts: int
    base_delay: timedelta
    max_delay: timedelta

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def delay(self, attempt: int, error: BrokerError) -> timedelta:
        backoff: timedelta = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        if isinstance(error, RateLimitedError) and error.retry_after is not None:
            backoff = max(backoff, min(error.retry_after, self.max_delay))
        return backoff


_STD = ReadRetryPolicy(3, timedelta(milliseconds=500), timedelta(seconds=4))
_LOOKUP = ReadRetryPolicy(4, timedelta(milliseconds=500), timedelta(seconds=4))
_SNAPSHOT = ReadRetryPolicy(2, timedelta(milliseconds=500), timedelta(seconds=2))

DEFAULT_READ_POLICIES: Final[Mapping[str, ReadRetryPolicy]] = {
    "account": _STD,
    "account_config": _STD,
    "clock": _STD,
    "calendar": _STD,
    "positions": _STD,
    "orders": _STD,
    "order_by_client_id": _LOOKUP,  # recovery lookups matter most
    "activities": _STD,
    "marks": _SNAPSHOT,  # marks are re-fetched often; stale beats slow
    "session_closes": _STD,
    "daily_bars": _STD,
}

Sleeper = Callable[[timedelta], None]


def real_sleep(delay: timedelta) -> None:  # pragma: no cover - exercised live only
    time.sleep(delay.total_seconds())


def with_read_retries[T](fn: Callable[[], T], policy: ReadRetryPolicy, sleep: Sleeper) -> T:
    attempt = 1
    while True:
        try:
            return fn()
        except (RateLimitedError, ServiceUnavailableError) as exc:
            if attempt >= policy.max_attempts:
                raise
            sleep(policy.delay(attempt, exc))
            attempt += 1
