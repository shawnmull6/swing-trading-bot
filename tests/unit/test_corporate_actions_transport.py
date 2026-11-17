"""Step 16.16B Phase 1: the single fixed ``/v1/corporate-actions`` GET on the transport.

Offline: a scripted session, fake credentials, no network."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from swing_trading_bot.broker import alpaca_http
from swing_trading_bot.broker.alpaca_http import (
    ALLOWED_ORIGINS,
    CORPORATE_ACTIONS_PATH,
    CORPORATE_ACTIONS_SERVICE,
    AlpacaReadTransport,
)
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
)
from tests.unit.test_alpaca_http import FAKE_KEY, FAKE_SECRET, response, transport

CA_URL = "https://data.alpaca.markets/v1/corporate-actions"
PARAMS = {"symbols": "AAPL,SPY", "types": "forward_split,cash_dividend",
          "start": "2020-07-01", "end": "2021-06-30", "limit": "5", "sort": "asc"}  # fmt: skip


def ok(body: str = '{"corporate_actions": {}, "next_page_token": null}') -> requests.Response:
    r = response(200, body)
    r.url = CA_URL
    return r


def test_corporate_actions_request_is_exact() -> None:
    body = '{"corporate_actions": {"cash_dividends": [{"rate": 0.205}]}, "next_page_token": null}'
    t, s = transport(ok(body))
    out = t.get_corporate_actions_json(PARAMS)
    assert isinstance(out, dict)
    assert out["corporate_actions"]["cash_dividends"][0]["rate"] == Decimal("0.205")
    (call,) = s.calls
    assert call["url"] == CA_URL
    assert call["params"] == PARAMS
    assert call["allow_redirects"] is False
    assert call["timeout"] == (5, 20)
    assert set(call["headers"]) == {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "User-Agent"}
    assert s.trust_env is False  # type: ignore[attr-defined]  # no env proxies / .netrc


def test_endpoint_constants_are_the_data_host_v1_path_only() -> None:
    assert CORPORATE_ACTIONS_SERVICE == "data"
    assert CORPORATE_ACTIONS_PATH == "/v1/corporate-actions"
    assert ALLOWED_ORIGINS[CORPORATE_ACTIONS_SERVICE] + CORPORATE_ACTIONS_PATH == CA_URL
    # no version, path or host parameter: only the query parameters are caller-chosen
    sig = inspect.signature(AlpacaReadTransport.get_corporate_actions_json)
    assert list(sig.parameters) == ["self", "params"]


def test_get_json_cannot_reach_v1_or_another_version() -> None:
    t, s = transport(ok())
    t.get_json("data", "/v1/corporate-actions", {})
    assert s.calls[0]["url"] == "https://data.alpaca.markets/v2/v1/corporate-actions"
    bad_paths = (
        "/../v1/corporate-actions",
        "v1/corporate-actions",
        "https://data.alpaca.markets/v1/corporate-actions",
        "/corporate-actions?x=1",
    )
    for bad in bad_paths:
        with pytest.raises(ValueError, match="invalid API path"):
            t.get_json("data", bad, {})
    assert len(s.calls) == 1


def test_only_one_non_v2_path_exists_in_the_transport() -> None:
    source = Path(alpaca_http.__file__).read_text()
    literals = {n.value for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}  # fmt: skip
    versioned = {v for v in literals if v.startswith(("/v1", "/v1beta", "/v3"))}
    assert versioned == {"/v1/corporate-actions"}
    assert not any("corporate_actions/announcements" in v for v in literals)  # no fallback


def test_non_string_parameters_are_refused_before_sending() -> None:
    t, s = transport(ok())
    with pytest.raises(ValueError, match="pre-formatted"):
        t.get_corporate_actions_json({"limit": 5})  # type: ignore[dict-item]
    assert s.calls == []


def test_status_taxonomy_is_shared() -> None:
    entitlement = '{"message": "subscription does not permit querying recent SIP data"}'
    cases: list[tuple[int, str, type[Exception]]] = [
        (401, "{}", AuthenticationError),
        (403, entitlement, MarketDataEntitlementError),
        (403, '{"message": "forbidden"}', AuthenticationError),
        (404, '{"message": "not found"}', OrderNotFoundError),
        (429, "{}", RateLimitedError),
        (503, "{}", ServiceUnavailableError),
        (302, "", InvalidBrokerDataError),
    ]
    for status, body, error in cases:
        r = response(status, body)
        r.url = CA_URL
        t, _ = transport(r)
        with pytest.raises(error) as info:
            t.get_corporate_actions_json(PARAMS)
        assert FAKE_KEY not in str(info.value) and FAKE_SECRET not in str(info.value)


def test_response_from_another_host_is_refused() -> None:
    r = response(200, "{}")
    r.url = "https://paper-api.alpaca.markets/v1/corporate-actions"
    t, _ = transport(r)
    with pytest.raises(InvalidBrokerDataError, match="unexpected host"):
        t.get_corporate_actions_json(PARAMS)
