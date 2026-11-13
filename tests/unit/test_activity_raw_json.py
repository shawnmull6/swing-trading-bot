"""Step 15B.5B.1: canonical raw-JSON serialization of broker activities.

``decode_json`` decodes a JSON number with a fraction/exponent as ``Decimal``; the old
``raw_json`` (plain ``json.dumps``) raised ``TypeError`` on it before field validation, so
a JSON number anywhere in a non-trade activity ended a CLI/runtime command with exit 1
(INTERNAL) and no ``BROKER_PAYLOAD`` halt. Now:

* a finite ``Decimal`` is written as its exact JSON number (valid JSON, deterministic);
* NaN / ±Infinity (float or Decimal) are not JSON -> ``InvalidBrokerDataError``;
* values the decoder can never produce (sets, bytes, objects, non-string keys) still raise
  ``TypeError``: a programming defect, never reclassified as a broker payload;
* output is byte-identical to the previous ``json.dumps`` for every payload it accepted,
  so stored ``raw_json`` and idempotent re-ingestion are unaffected;
* field validation is unchanged: money fields still require strings.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.app.cli import exit_code_for
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.broker.alpaca_http import decode_json
from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    payload_cause,
)
from swing_trading_bot.broker.mapping import activity_from_raw, raw_json

D = Decimal
NON_FINITE: list[object] = [math.nan, math.inf, -math.inf, D("NaN"), D("Infinity"),
                            D("-Infinity"), D("sNaN")]  # fmt: skip
CASH: dict[str, Any] = {
    "id": "20261110000000000::00000001", "activity_type": "CSD", "date": "2026-11-10",
    "net_amount": "100.10", "status": "executed", "description": "",
}  # fmt: skip
FILL: dict[str, Any] = {
    "id": "20261110000000000::00000002", "activity_type": "FILL",
    "transaction_time": "2026-11-10T15:00:00Z", "symbol": "AAA", "side": "buy",
    "qty": "10", "price": "12.5", "order_id": "o-1", "cum_qty": "10", "leaves_qty": "0",
    "order_status": "filled", "type": "fill",
}  # fmt: skip


def legacy(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ============================================================ byte identity / determinism
json_scalars = (
    st.none() | st.booleans() | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False) | st.text()
)  # fmt: skip
json_values = st.recursive(
    json_scalars,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=20,
)


@given(st.dictionaries(st.text(), json_values, max_size=6))
def test_identical_to_previous_encoding_for_every_previously_accepted_payload(
    raw: dict[str, Any],
) -> None:
    assert raw_json(raw) == legacy(raw)


@given(st.dictionaries(st.text(), json_values, max_size=6))
def test_tuples_encode_like_lists(raw: dict[str, Any]) -> None:
    as_tuples = {k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()}
    assert raw_json(as_tuples) == legacy(raw)


def _has_fraction_or_exponent(d: Decimal) -> bool:
    return not str(d).lstrip("-").isdigit()  # decode_json makes Decimal only from these


# What decode_json can yield for valid JSON: no float (a fraction/exponent -> Decimal).
decoded_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text()
    | st.decimals(allow_nan=False, allow_infinity=False).filter(_has_fraction_or_exponent),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=20,
)  # fmt: skip


@given(st.dictionaries(st.text(), decoded_values, max_size=6))
def test_decoded_payloads_round_trip_exactly_and_canonically(raw: dict[str, Any]) -> None:
    text = raw_json(raw)
    decoded = decode_json(text)  # valid JSON: the production decoder accepts it
    assert decoded == raw  # numerically exact: Decimals are never routed through float
    assert raw_json(decoded) == text  # canonical: re-encoding is a fixed point


@pytest.mark.parametrize(
    ("value", "literal"),
    [(D("1.50"), "1.50"), (D("1E+5"), "1E+5"), (D("-0.0"), "-0.0"), (D("0E-7"), "0E-7"),
     (D("0.1000000000000000000000001"), "0.1000000000000000000000001"),
     (D("1E+999"), "1E+999")],
)  # fmt: skip
def test_finite_decimals_are_written_as_exact_json_numbers(value: Decimal, literal: str) -> None:
    assert raw_json({"x": value}) == '{"x":' + literal + "}"
    assert decode_json(raw_json({"x": value})) == {"x": value}


def test_decimals_from_the_production_decoder() -> None:
    raw = decode_json('{"b":[1.50,{"c":2.5e3}],"a":0.1}')
    assert isinstance(raw, dict)
    assert raw_json(raw) == '{"a":0.1,"b":[1.50,{"c":2.5E+3}]}'


def test_strings_are_never_changed_into_numbers_or_vice_versa() -> None:
    assert raw_json({"n": "1.50", "m": D("1.50")}) == '{"m":1.50,"n":"1.50"}'


# ============================================================ non-JSON values -> payload error
@pytest.mark.parametrize("bad", NON_FINITE)
@pytest.mark.parametrize("where", ["top", "list", "nested"])
def test_non_finite_numbers_are_payload_errors(bad: object, where: str) -> None:
    raw: dict[str, dict[str, Any]] = {
        "top": {"x": bad},
        "list": {"x": [1, bad]},
        "nested": {"x": {"y": [{"z": bad}]}},
    }
    with pytest.raises(InvalidBrokerDataError, match="non-finite"):
        raw_json(raw[where])


def test_non_finite_from_the_production_decoder_is_a_payload_error() -> None:
    for text in ('{"x":NaN}', '{"x":[Infinity]}', '{"x":{"y":-Infinity}}'):
        raw = decode_json(text)  # the stdlib decoder accepts these non-JSON constants
        assert isinstance(raw, dict)
        with pytest.raises(InvalidBrokerDataError):
            raw_json(raw)


# ============================================================ programming defects stay defects
@pytest.mark.parametrize(
    "bad",
    [{1, 2}, b"bytes", object(), datetime(2026, 1, 1, tzinfo=UTC), {1: "int key"},
     {None: "none key"}],
)  # fmt: skip
def test_values_the_decoder_cannot_produce_remain_type_errors(bad: object) -> None:
    with pytest.raises(TypeError) as info:
        raw_json({"x": bad})
    assert not isinstance(info.value, InvalidBrokerDataError)
    assert payload_cause(info.value) is None
    assert exit_code_for(info.value) is ExitCode.INTERNAL


def test_a_programming_defect_inside_activity_mapping_is_not_reclassified() -> None:
    with pytest.raises(TypeError):
        activity_from_raw({**CASH, "extra": {"a", "set"}})


# ============================================================ activity mapping
@pytest.mark.parametrize(
    "extra",
    [{"fee_rate": D("0.000166")}, {"count": 3}, {"nested": {"a": [D("1.5"), 2, "x"]}},
     {"big": D("123456789012345678901234567890.123456789")}],
)  # fmt: skip
def test_valid_numbers_in_unmapped_fields_are_kept_exactly(extra: dict[str, Any]) -> None:
    a = activity_from_raw({**CASH, **extra})
    assert decode_json(a.raw_json) == {**CASH, **extra}
    assert a.net_amount == D("100.10") and a.description == ""  # mapped fields unchanged


@pytest.mark.parametrize("key", ["net_amount", "qty", "per_share_amount"])
@pytest.mark.parametrize("value", [D("1.5"), D("100"), 7, 0])
def test_mapped_money_fields_still_require_strings(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        activity_from_raw({**CASH, key: value})


@pytest.mark.parametrize("key", ["qty", "price", "cum_qty", "leaves_qty"])
def test_fill_money_fields_still_require_strings(key: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        activity_from_raw({**FILL, key: D("10.5")})


def test_fill_with_a_valid_unmapped_number_maps_normally() -> None:
    a = activity_from_raw({**FILL, "swap_rate": D("0.25")})
    assert a.qty == D(10) and a.price == D("12.5")
    assert '"swap_rate":0.25' in a.raw_json


@pytest.mark.parametrize("base", [CASH, FILL])
@pytest.mark.parametrize("bad", [math.nan, D("Infinity")])
def test_non_finite_in_any_activity_is_a_payload_error(base: dict[str, Any], bad: object) -> None:
    with pytest.raises(InvalidBrokerDataError):
        activity_from_raw({**base, "unmapped": bad})


def test_decoded_numeric_net_amount_is_a_payload_error_not_a_type_error() -> None:
    raw = decode_json(json.dumps({**CASH, "net_amount": 1.5}))  # -> Decimal("1.5")
    assert isinstance(raw, dict)
    with pytest.raises(InvalidBrokerDataError, match="net_amount"):
        activity_from_raw(raw)


def test_string_money_keeps_exact_decimal_processing() -> None:
    a = activity_from_raw({**CASH, "net_amount": "-0.0001", "qty": "1.000", "per_share_amount":
                           "0.123456"})  # fmt: skip
    assert (a.net_amount, a.qty, a.per_share_amount) == (D("-0.0001"), D("1.000"), D("0.123456"))
    assert str(a.qty) == "1.000"


@pytest.mark.parametrize(("desc", "expected"), [("", ""), (None, None), ("REG", "REG")])
def test_description_rule_is_unchanged(desc: str | None, expected: str | None) -> None:
    a = activity_from_raw({**CASH, "description": desc})
    assert a.description == expected
    assert json.loads(a.raw_json)["description"] == desc


def test_numeric_description_is_still_a_description_error() -> None:
    with pytest.raises(InvalidBrokerDataError, match="description"):
        activity_from_raw({**CASH, "description": D("1.5")})


# ============================================================ classification
def wrapped(inner: BaseException) -> IncompleteRetrievalError:
    outer = IncompleteRetrievalError("activity window interrupted on page 2")
    outer.__cause__ = inner
    return outer


def test_serialization_payload_errors_classify_as_exit_10_direct_and_wrapped() -> None:
    with pytest.raises(InvalidBrokerDataError) as info:
        activity_from_raw({**CASH, "x": math.nan})
    assert exit_code_for(info.value) is ExitCode.BROKER_PAYLOAD
    assert exit_code_for(wrapped(info.value)) is ExitCode.BROKER_PAYLOAD
    assert exit_code_for(wrapped(TypeError("defect"))) is ExitCode.BROKER_UNAVAILABLE
    assert exit_code_for(TypeError("defect")) is ExitCode.INTERNAL
