"""Step 15A: the production read-only broker factory and the execution boundary.

Structural (AST/attribute) and runtime proofs that the Alpaca path can only issue HTTPS
GET requests to the allowlisted paper hosts. Synthetic credentials; no network."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from swing_trading_bot.app.broker_factory import (
    account_identity_problems,
    mask_account_id,
    production_broker_factory,
)
from swing_trading_bot.broker import alpaca_http
from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS, AlpacaReadTransport
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.oms.execution import ExecutionNotPermittedError, SimulationExecution
from tests.support.alpaca_account import ACCOUNT_ID, AccountSession, FakeAlpacaAccount

SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
ENV = {
    "ALPACA_API_KEY": "PKSYNTHETICKEY0000000001",
    "ALPACA_SECRET_KEY": "synthetic/Secret/Value/NotReal/0000000000001",
    "TRADING_MODE": "paper",
}
WRITE_VERBS = ("post", "put", "patch", "delete")


def account(**changes: Any) -> AccountSnapshot:
    base: dict[str, Any] = {
        "as_of": datetime(2026, 11, 9, tzinfo=UTC), "account_id": ACCOUNT_ID,
        "environment": "paper", "status": "ACTIVE", "currency": "USD", "cash": 0,
        "non_marginable_buying_power": 0, "multiplier": 1, "pattern_day_trader": None,
        "trading_blocked": False, "account_blocked": False, "accrued_fees": 0,
        "pending_reg_taf_fees": 0,
    }  # fmt: skip
    base.update(changes)
    return AccountSnapshot(**base)


# ============================================================ structural proofs
def test_no_sdk_trading_client_and_no_write_verbs_anywhere() -> None:
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name != "TradingClient", path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("alpaca.trading"), path
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_VERBS
                and path.parent.name == "broker"
            ):
                raise AssertionError(f"{path.name} calls .{node.func.attr}()")


def test_factory_builds_only_the_read_adapter() -> None:
    tree = ast.parse((SRC / "app" / "broker_factory.py").read_text())
    constructed = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id[0].isupper()
    }
    assert constructed == {"AlpacaPaperReadAdapter", "AlpacaReadTransport"}


def test_transport_and_adapter_expose_no_write_surface() -> None:
    public = {n for n, _ in inspect.getmembers(AlpacaReadTransport) if not n.startswith("_")}
    assert public == {"get_json"}
    for name in ("submit_order", "cancel_order", "replace_order", "close_position"):
        assert not hasattr(AlpacaPaperReadAdapter, name)


def test_allowlist_is_https_paper_only() -> None:
    assert dict(ALLOWED_ORIGINS) == {
        "trading": "https://paper-api.alpaca.markets",
        "data": "https://data.alpaca.markets",
    }
    alpaca_http.verify_allowlist()


def test_changed_sdk_base_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        alpaca_http, "BASE_URLS", {"trading": "https://api.alpaca.markets", "data": "x"}
    )
    with pytest.raises(ValueError, match="allowlisted"):
        production_broker_factory(ENV, session_factory=requests.Session)()


# ============================================================ runtime proofs
def adapter_for(account_state: FakeAlpacaAccount) -> tuple[AlpacaPaperReadAdapter, Any]:
    session = AccountSession(account_state)
    make = production_broker_factory(ENV, session_factory=lambda: session,  # type: ignore[arg-type,return-value]
                                     clock=account_state.clock)  # fmt: skip
    return make(), session


def test_every_request_is_https_get_to_an_allowlisted_host() -> None:
    adapter, session = adapter_for(FakeAlpacaAccount())
    adapter.get_account()
    adapter.get_account_config()
    adapter.get_clock()
    adapter.get_positions()
    adapter.get_open_orders()
    adapter.get_order_by_client_id("stb-unknown")
    assert session.trust_env is False  # no environment proxies or .netrc
    assert session.requests and all(m == "GET" for m, _ in session.requests)
    assert all(
        url.startswith((ALLOWED_ORIGINS["trading"] + "/", ALLOWED_ORIGINS["data"] + "/"))
        for _, url in session.requests
    )
    assert {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY"} <= session.header_names


def test_redirects_and_foreign_hosts_are_refused() -> None:
    class Redirecting:
        trust_env = True

        def get(self, url: str, **_kw: Any) -> requests.Response:
            r = requests.Response()
            r.status_code, r.url, r._content = 302, "https://api.alpaca.markets/v2/account", b""
            return r

    make = production_broker_factory(ENV, session_factory=Redirecting)  # type: ignore[arg-type]
    with pytest.raises(InvalidBrokerDataError, match="unexpected host"):
        make().get_account()


def test_read_adapter_can_never_become_an_execution_capability() -> None:
    adapter, _ = adapter_for(FakeAlpacaAccount())
    with pytest.raises(ExecutionNotPermittedError):
        SimulationExecution(adapter)  # type: ignore[arg-type]


# ============================================================ identity
@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"environment": "live"}, "environment"),
        ({"account_id": "PA12345"}, "UUID"),
        ({"account_id": ACCOUNT_ID.upper()}, "UUID"),
        ({"status": "ACCOUNT_CLOSED"}, "status"),
        ({"currency": "EUR"}, "currency"),
    ],
)
def test_uncertain_identity_fails_closed(changes: dict[str, Any], problem: str) -> None:
    problems = account_identity_problems(account(**changes), environment="paper")
    assert any(problem in p for p in problems)
    assert all(ACCOUNT_ID not in p for p in problems)


def test_valid_identity_and_masking() -> None:
    assert account_identity_problems(account(), environment="paper") == []
    assert mask_account_id(ACCOUNT_ID) == "…" + ACCOUNT_ID[-4:]
    assert ACCOUNT_ID not in mask_account_id(ACCOUNT_ID)
