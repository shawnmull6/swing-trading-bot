"""Step 9: GET-only transport, status mapping, exact JSON decoding and static safety."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from alpaca.common import constants as sdk_constants
from alpaca.common.rest import RESTClient

from swing_trading_bot.broker import alpaca_http
from swing_trading_bot.broker.alpaca_http import (
    AlpacaReadTransport,
    ReadRetryPolicy,
    decode_json,
    raise_for_status,
)
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerRequestError,
    InvalidBrokerDataError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.config.settings import settings_from_mapping

SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "broker"
FAKE_KEY = "PKFAKETESTKEY000000001"
FAKE_SECRET = "fakeTestSecretValue/DoNotUse/0000000000001"
SETTINGS = settings_from_mapping(
    {"ALPACA_API_KEY": FAKE_KEY, "ALPACA_SECRET_KEY": FAKE_SECRET, "TRADING_MODE": "paper"}
)


def response(
    status: int, body: str = "{}", headers: dict[str, str] | None = None
) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.headers.update(headers or {})
    return r


class FakeSession:
    def __init__(self, *results: requests.Response | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def transport(*results: requests.Response | Exception) -> tuple[AlpacaReadTransport, FakeSession]:
    session = FakeSession(*results)
    return AlpacaReadTransport(SETTINGS, session=session), session  # type: ignore[arg-type]


def test_get_request_shape_and_exact_decoding() -> None:
    t, s = transport(response(200, '{"p": 150.2501, "s": 100, "x": "V"}'))
    out = t.get_json("data", "/stocks/trades/latest", {"symbols": "AAPL"})
    assert isinstance(out, dict)
    assert isinstance(out["p"], Decimal)
    assert out == {"p": Decimal("150.2501"), "s": 100, "x": "V"}
    (call,) = s.calls
    assert call["url"] == "https://data.alpaca.markets/v2/stocks/trades/latest"
    assert call["allow_redirects"] is False
    assert call["timeout"] == (5, 20)
    assert call["params"] == {"symbols": "AAPL"}
    assert set(call["headers"]) == {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "User-Agent"}
    t2, s2 = transport(response(200, "[]"))
    t2.get_json("trading", "/account/activities", {})
    assert s2.calls[0]["url"] == "https://paper-api.alpaca.markets/v2/account/activities"


def test_secrets_never_in_repr() -> None:
    t, _ = transport()
    assert FAKE_KEY not in repr(t) and FAKE_SECRET not in repr(t)
    assert "read-only" in repr(AlpacaPaperReadAdapter(t))


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthenticationError), (403, AuthenticationError), (404, OrderNotFoundError),
     (429, RateLimitedError), (500, ServiceUnavailableError), (503, ServiceUnavailableError),
     (504, ServiceUnavailableError), (302, InvalidBrokerDataError), (400, BrokerRequestError),
     (422, BrokerRequestError)],
)  # fmt: skip
def test_status_mapping(status: int, error: type[Exception]) -> None:
    t, _ = transport(response(status, '{"code": 1, "message": "nope"}'))
    with pytest.raises(error):
        t.get_json("trading", "/account", {})


def test_auth_error_message_carries_no_body() -> None:
    with pytest.raises(AuthenticationError) as info:
        raise_for_status(response(401, f'{{"message": "bad key {FAKE_KEY}"}}'), "GET /account")
    assert FAKE_KEY not in str(info.value)


def test_retry_after_header() -> None:
    t, _ = transport(response(429, "{}", {"Retry-After": "7"}))
    with pytest.raises(RateLimitedError) as info:
        t.get_json("trading", "/account", {})
    assert info.value.retry_after is not None and info.value.retry_after.total_seconds() == 7


@pytest.mark.parametrize(
    "exc", [requests.Timeout("slow"), requests.ConnectionError("reset"),
            requests.exceptions.ChunkedEncodingError("broken")],
)  # fmt: skip
def test_transport_failures_are_service_unavailable(exc: Exception) -> None:
    t, _ = transport(exc)
    with pytest.raises(ServiceUnavailableError):
        t.get_json("trading", "/clock", {})


def test_invalid_json_and_paths() -> None:
    t, _ = transport(response(200, "{not json"))
    with pytest.raises(InvalidBrokerDataError, match="not valid JSON"):
        t.get_json("trading", "/clock", {})
    for bad in ("account", "/../orders", "https://evil.example/x", "/orders?x=1"):
        with pytest.raises(ValueError, match="invalid API path"):
            t.get_json("trading", bad, {})
    assert decode_json("1.10") == Decimal("1.10")


def test_retry_policy_validation_and_delays() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        ReadRetryPolicy(0, timedelta(0), timedelta(0))
    p = alpaca_http.DEFAULT_READ_POLICIES["account"]
    assert [p.delay(i, ServiceUnavailableError("x")).total_seconds() for i in (1, 2, 3, 4)] == [
        0.5, 1, 2, 4]  # fmt: skip


def test_no_network_in_normal_tests() -> None:
    adapter = AlpacaPaperReadAdapter.from_settings(SETTINGS, sleep=lambda _: None)
    with pytest.raises((ServiceUnavailableError, RuntimeError)):  # socket access is blocked
        adapter.get_clock()


# --------------------------------------------------------- static safety


WRITE_NAMES = {
    "post", "put", "patch", "delete", "request", "submit_order", "cancel_order",
    "cancel_order_by_id", "cancel_orders", "replace_order", "replace_order_by_id",
    "close_position", "close_all_positions", "exercise_options_position", "_request",
}  # fmt: skip


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text())


@pytest.mark.parametrize("module", ["alpaca_read.py", "alpaca_http.py", "activity_feed.py"])
def test_alpaca_modules_cannot_write(module: str) -> None:
    tree = _tree(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in WRITE_NAMES, f"{module}:{node.lineno} uses .{node.attr}"
        if isinstance(node, ast.FunctionDef):
            assert node.name not in WRITE_NAMES, f"{module}:{node.lineno} defines {node.name}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.upper() not in {"POST", "PUT", "PATCH", "DELETE"}, module
        if isinstance(node, ast.Name):
            assert node.id not in {"TradingClient", "StockHistoricalDataClient"}, module


def test_transport_exposes_only_get_json() -> None:
    public = {n for n in vars(AlpacaReadTransport) if not n.startswith("_")}
    assert public == {"get_json"}
    t, _ = transport()
    assert not any(isinstance(v, RESTClient) for v in vars(t).values())  # SDK client not kept


def test_only_the_transport_imports_sdk_or_http_libraries() -> None:
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] in {"alpaca", "requests"}:
                    assert path.name == "alpaca_http.py", f"{path.name} imports {name}"


def test_sdk_retry_behaviour_is_pinned() -> None:
    """Documents why an M2 write path must not use the SDK's request loop: it retries
    429/504 for every verb and ignores retry_attempts=0 (alpaca-py 0.45)."""
    assert sdk_constants.DEFAULT_RETRY_EXCEPTION_CODES == [429, 504]
    client = RESTClient("https://paper-api.alpaca.markets", api_key="k", secret_key="s",
                        retry_attempts=0, retry_exception_codes=[])  # fmt: skip
    assert client._retry == 3
    assert client._retry_codes == [429, 504]
