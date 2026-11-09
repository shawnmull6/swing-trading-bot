"""Step 8: broker error taxonomy, raw mapping and structural safety guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from swing_trading_bot.broker import mapping
from swing_trading_bot.broker.errors import (
    AmbiguousSubmissionError,
    AuthenticationError,
    BrokerError,
    BrokerRequestError,
    DuplicateClientOrderIdError,
    InvalidBrokerDataError,
    NotCancelableError,
    OrderNotFoundError,
    OrderRejectedError,
    OrderState,
    RateLimitedError,
    ServiceUnavailableError,
    submit_failure_state,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"


@pytest.mark.parametrize(
    ("cls", "state", "read_retryable", "after_submit"),
    [
        (OrderRejectedError, OrderState.NOT_CREATED, False, OrderState.NOT_CREATED),
        (DuplicateClientOrderIdError, OrderState.EXISTS, False, OrderState.EXISTS),
        (NotCancelableError, OrderState.EXISTS, False, OrderState.UNKNOWN),
        (AmbiguousSubmissionError, OrderState.UNKNOWN, False, OrderState.UNKNOWN),
        (AuthenticationError, OrderState.NOT_APPLICABLE, False, OrderState.UNKNOWN),
        (RateLimitedError, OrderState.NOT_APPLICABLE, True, OrderState.UNKNOWN),
        (ServiceUnavailableError, OrderState.NOT_APPLICABLE, True, OrderState.UNKNOWN),
        (BrokerRequestError, OrderState.NOT_APPLICABLE, False, OrderState.UNKNOWN),
        (InvalidBrokerDataError, OrderState.NOT_APPLICABLE, False, OrderState.UNKNOWN),
        (OrderNotFoundError, OrderState.NOT_APPLICABLE, False, OrderState.UNKNOWN),
    ],
)
def test_error_taxonomy(
    cls: type[BrokerError], state: OrderState, read_retryable: bool, after_submit: OrderState
) -> None:
    err = cls("x")
    assert isinstance(err, BrokerError)
    assert (err.order_state, err.read_retryable) == (state, read_retryable)
    # Only explicit rejection / duplicate are authoritative after a failed submit.
    assert submit_failure_state(err) is after_submit
    assert submit_failure_state(TimeoutError()) is OrderState.UNKNOWN
    assert not hasattr(err, "retryable")  # no generic "safe to retry" flag exists


def test_ambiguity_is_never_a_rejection() -> None:
    assert not issubclass(AmbiguousSubmissionError, OrderRejectedError)
    assert not issubclass(DuplicateClientOrderIdError, OrderRejectedError)


def test_mapping_parses_exact_values() -> None:
    raw = {
        "id": "20261109150100000000::0000", "activity_type": "FILL",
        "transaction_time": "2026-11-09T15:01:00.123456Z", "type": "partial_fill",
        "price": "100.0001", "qty": "3", "side": "sell", "symbol": "AAPL",
        "leaves_qty": "7", "cum_qty": "3", "order_id": "o-1", "order_status": "partially_filled",
    }  # fmt: skip
    a = mapping.activity_from_raw(raw)
    assert str(a.price) == "100.0001" and a.occurred_at is not None
    assert a.occurred_at.utcoffset() is not None
    assert mapping.raw_json(raw) == a.raw_json
    non_trade = mapping.activity_from_raw(
        {"id": "x", "activity_type": "INT", "date": "2026-11-09", "net_amount": "0.01"}
    )
    assert non_trade.occurred_at is None and not non_trade.is_fill
    with pytest.raises(InvalidBrokerDataError, match="date"):
        mapping.activity_from_raw({"id": "x", "activity_type": "INT", "date": "11/09/2026",
                                   "net_amount": "0.01"})  # fmt: skip
    with pytest.raises(InvalidBrokerDataError, match="ISO-8601"):
        mapping.timestamp({"t": "yesterday"}, "t")
    with pytest.raises(InvalidBrokerDataError, match="non-empty"):
        mapping.text({"t": ""}, "t")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_broker_package_has_no_sdk_network_or_ledger_imports() -> None:
    banned = ("alpaca", "socket", "http", "urllib", "requests", "httpx", "ssl")
    transport_only = {"alpaca", "requests"}  # confined to the GET-only transport (step 9)
    for path in sorted((SRC / "broker").rglob("*.py")):
        for name in _imports(path):
            root = name.split(".")[0]
            if path.name == "alpaca_http.py" and root in transport_only:
                continue
            assert root not in banned, f"{path}: {name}"
            assert not name.startswith("swing_trading_bot.ledger"), f"{path}: {name}"


def test_execution_port_is_only_implemented_by_sim() -> None:
    """Structural safety (§5.2): no module that implements submit/cancel touches Alpaca."""
    implementers = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                if {"submit_order", "cancel_order"} <= methods:
                    implementers.append(f"{path.relative_to(SRC)}:{node.name}")
    assert implementers == ["broker/ports.py:BrokerExecutionPort", "broker/sim.py:SimBroker"]


def test_buying_power_is_never_read_outside_raw_payloads() -> None:
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                assert node.attr != "buying_power", f"{path}:{node.lineno}"
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                assert node.slice.value != "buying_power", f"{path}:{node.lineno}"
