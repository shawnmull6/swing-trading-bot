"""Step 15B.5B: activity ``description`` compatibility and payload-cause classification.

* ``description`` (non-trade activities): absent/``null`` -> ``None``; any string,
  **including ""** (sent by Alpaca in Step 15B live run #3), kept exactly; any other JSON
  type -> ``InvalidBrokerDataError``. ``text``/``opt_text`` are unchanged for every
  other field.
* ``payload_cause`` finds an ``InvalidBrokerDataError`` directly or behind (at most a
  few) ``IncompleteRetrievalError`` wrappers, and nothing else.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.cli import exit_code_for
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.broker.errors import (
    PAYLOAD_CAUSE_MAX_DEPTH,
    AuthenticationError,
    BrokerError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UnknownBrokerStatusError,
    payload_cause,
)
from swing_trading_bot.broker.mapping import activity_from_raw, opt_text, text

PKG = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
ROW: dict[str, Any] = {
    "id": "20261110000000000::00000001", "activity_type": "CSD", "date": "2026-11-10",
    "created_at": "2026-11-10T15:00:00.000000Z", "currency": "USD", "net_amount": "100",
    "status": "executed", "description": "deposit",
}  # fmt: skip


def parse(**changes: Any) -> Any:
    return activity_from_raw({**ROW, **changes})


# ============================================================ description rule
def test_missing_description_is_none() -> None:
    raw = {k: v for k, v in ROW.items() if k != "description"}
    assert activity_from_raw(raw).description is None


def test_null_description_is_none() -> None:
    assert parse(description=None).description is None


def test_empty_description_is_kept_exactly_and_differs_from_none() -> None:
    a = parse(description="")
    assert a.description == "" and a.description is not None
    assert json.loads(a.raw_json)["description"] == ""


@pytest.mark.parametrize("value", ["REG", " ", "TAF fee", "ünïcode"])
def test_non_empty_description_is_kept_exactly(value: str) -> None:
    a = parse(description=value)
    assert a.description == value and json.loads(a.raw_json)["description"] == value


@pytest.mark.parametrize("value", [True, False, 0, 1, 1.5, Decimal("1.5"), [], ["x"], {}, {"a": 1}])
def test_non_string_description_is_a_payload_violation(value: object) -> None:
    # Decimal: what decode_json(parse_float=Decimal) yields for a JSON float.
    with pytest.raises(InvalidBrokerDataError, match="description"):
        parse(description=value)


# ============================================================ every other field stays strict
@pytest.mark.parametrize("key", ["id", "activity_type"])
@pytest.mark.parametrize("value", ["", None, 5])
def test_required_text_fields_stay_strict(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: value})


@pytest.mark.parametrize("key", ["symbol", "order_id"])
@pytest.mark.parametrize("value", ["", 5, True])
def test_optional_text_fields_still_reject_empty_and_non_strings(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        parse(**{key: value})


def test_global_text_helpers_are_unchanged() -> None:
    with pytest.raises(InvalidBrokerDataError):
        text({"k": ""}, "k")
    with pytest.raises(InvalidBrokerDataError):
        opt_text({"k": ""}, "k")
    assert opt_text({}, "k") is None and text({"k": "v"}, "k") == "v"


def test_relaxed_rule_is_used_for_description_only() -> None:
    tree = ast.parse((PKG / "broker" / "mapping.py").read_text())
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id == "_activity_description"
    }  # fmt: skip
    assert callers == {"activity_from_raw"}
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert "description" in strings
    src = (PKG / "broker" / "mapping.py").read_text()
    assert 'opt_text(raw, "description")' not in src


# ============================================================ payload_cause
def wrap(inner: BaseException, depth: int = 1) -> IncompleteRetrievalError:
    exc: BaseException = inner
    for n in range(depth):
        outer = IncompleteRetrievalError(f"interrupted {n}")
        outer.__cause__ = exc
        exc = outer
    assert isinstance(exc, IncompleteRetrievalError)
    return exc


def test_direct_payload_errors_are_recognized() -> None:
    e = InvalidBrokerDataError("x")
    assert payload_cause(e) is e
    u = UnknownBrokerStatusError("weird")
    assert payload_cause(u) is u


@pytest.mark.parametrize("depth", range(1, PAYLOAD_CAUSE_MAX_DEPTH + 1))
def test_wrapped_payload_errors_are_recognized_and_preserved(depth: int) -> None:
    inner = InvalidBrokerDataError("field 'description' must be a string")
    outer = wrap(inner, depth)
    assert payload_cause(outer) is inner
    assert isinstance(outer, IncompleteRetrievalError)  # nothing replaced or re-raised
    chain: list[BaseException] = []
    cur: BaseException | None = outer
    while cur is not None:
        chain.append(cur)
        cur = cur.__cause__
    assert chain[-1] is inner and len(chain) == depth + 1


def test_traversal_is_bounded() -> None:
    assert payload_cause(wrap(InvalidBrokerDataError("x"), PAYLOAD_CAUSE_MAX_DEPTH + 1)) is None


@pytest.mark.parametrize(
    "exc",
    [
        ServiceUnavailableError("timeout", kind="timeout"),
        ServiceUnavailableError("network", kind="network"),
        ServiceUnavailableError("HTTP 503", status_code=503),
        RateLimitedError("HTTP 429", status_code=429),
        AuthenticationError("HTTP 401", status_code=401),
        OrderNotFoundError("HTTP 404", status_code=404),
        BrokerError("generic"),
        IncompleteRetrievalError("activity pagination cursor did not advance"),
        IncompleteRetrievalError("activity window exceeded 500 pages"),
        ValueError("not a broker error"),
    ],
)
def test_unrelated_failures_are_not_payload_errors(exc: BaseException) -> None:
    assert payload_cause(exc) is None


@pytest.mark.parametrize(
    "cause",
    [ServiceUnavailableError("timeout", kind="timeout"), RateLimitedError("429"),
     ServiceUnavailableError("network", kind="network")],
)  # fmt: skip
def test_incomplete_retrieval_with_transient_cause_is_not_payload(cause: BaseException) -> None:
    assert payload_cause(wrap(cause)) is None


def test_only_explicit_causes_are_followed() -> None:
    outer = IncompleteRetrievalError("interrupted")
    outer.__context__ = InvalidBrokerDataError("implicit context only")
    assert payload_cause(outer) is None
    other = ValueError("not a wrapper")
    other.__cause__ = InvalidBrokerDataError("x")
    assert payload_cause(other) is None


# ============================================================ CLI exit codes
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (InvalidBrokerDataError("x"), ExitCode.BROKER_PAYLOAD),
        (wrap(InvalidBrokerDataError("x")), ExitCode.BROKER_PAYLOAD),
        (wrap(InvalidBrokerDataError("x"), 3), ExitCode.BROKER_PAYLOAD),
        (IncompleteRetrievalError("no cause"), ExitCode.BROKER_UNAVAILABLE),
        (wrap(ServiceUnavailableError("t", kind="timeout")), ExitCode.BROKER_UNAVAILABLE),
        (wrap(RateLimitedError("429")), ExitCode.BROKER_UNAVAILABLE),
        (ServiceUnavailableError("503", status_code=503), ExitCode.BROKER_UNAVAILABLE),
        (RateLimitedError("429"), ExitCode.BROKER_UNAVAILABLE),
        (AuthenticationError("401", status_code=401), ExitCode.BROKER_UNAVAILABLE),
    ],
)
def test_cli_exit_codes(exc: BaseException, code: ExitCode) -> None:
    assert exit_code_for(exc) is code
