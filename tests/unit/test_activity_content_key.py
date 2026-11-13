"""Step 15B.6C: ``ActivityRecord.content_key()`` compares raw payloads by exact JSON value.

Before: ``json.loads`` turned every fraction/exponent into a binary ``float``, so a value
change below float precision (``0.10000000000000000001`` -> ``0.1``) or between two
overflowing exponents (``1E+400`` -> ``2E+400``) compared equal and a changed replay was
recorded as a DUPLICATE instead of a CONFLICT. Step 15B.6C.1: Python's ``True == 1``
(and ``False == 0``) made a JSON boolean equal to a JSON number; booleans are now
distinct. Synthetic payloads only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from swing_trading_bot.broker.mapping import raw_json
from swing_trading_bot.domain.enums import ActivityAttribution
from swing_trading_bot.ledger.repositories import ActivityRecord

D = Decimal
T = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
FEE = D("-0.02")


def rec(raw: str, net_amount: Decimal | None = FEE) -> ActivityRecord:
    return ActivityRecord(
        activity_id="20261109150000000::fee",
        activity_type="FEE",
        ts=T,
        attribution=ActivityAttribution.ATTRIBUTABLE,
        raw_json=raw,
        ingested_at=T,
        net_amount=net_amount,
    )


def same(a: str, b: str) -> bool:
    return rec(a).content_key() == rec(b).content_key()


BIG = "123456789012345678901234567890123456789"  # well beyond 64 bits

EQUIVALENT = [
    pytest.param('{"x":0.1,"y":2}', '{"x":0.1,"y":2}', id="identical"),
    pytest.param('{"x":0.1,"y":2}', '{"y":2,"x":0.1}', id="key-order"),
    pytest.param('{"x":1.10}', '{"x":1.1}', id="trailing-zero"),
    pytest.param('{"x":1.0}', '{"x":1}', id="integral-fraction-vs-int"),
    pytest.param('{"x":1E+2}', '{"x":100}', id="exponent-vs-int"),
    pytest.param(f'{{"x":{BIG}}}', f'{{"x":{BIG}}}', id="big-int-identical"),
    pytest.param('{"n":{"a":[1.50,{"b":2E-3}]}}', '{"n":{"a":[1.5,{"b":0.002}]}}', id="nested"),
    pytest.param('{"x":true}', '{"x":true}', id="true-true"),
    pytest.param('{"x":false}', '{"x":false}', id="false-false"),
    pytest.param('{"x":null}', '{"x":null}', id="null-null"),
    pytest.param('{"x":[true,false],"y":1}', '{"y":1,"x":[true,false]}', id="bool-key-order"),
    pytest.param('{"n":{"a":[true,{"b":1.0}]}}', '{"n":{"a":[true,{"b":1}]}}', id="nested-bool"),
]
DISTINCT = [
    pytest.param('{"x":0.10000000000000000001}', '{"x":0.1}', id="below-float-precision"),
    pytest.param('{"x":1E+400}', '{"x":2E+400}', id="beyond-float-range"),
    pytest.param('{"x":1E-400}', '{"x":2E-400}', id="below-float-subnormal"),
    pytest.param(f'{{"x":{BIG}}}', f'{{"x":{BIG[:-1]}8}}', id="big-int-last-digit"),
    pytest.param('{"x":"0.1"}', '{"x":0.1}', id="string-vs-number"),
    pytest.param('{"x":"1"}', '{"x":1}', id="string-vs-int"),
    pytest.param('{"x":"0.1"}', '{"x":"0.10"}', id="string-spelling"),
    pytest.param('{"n":{"a":[0.10000000000000000001]}}', '{"n":{"a":[0.1]}}', id="nested"),
    pytest.param('{"x":0.1}', '{"x":0.1,"y":null}', id="added-key"),
    pytest.param('{"x":true}', '{"x":1}', id="true-vs-1"),
    pytest.param('{"x":false}', '{"x":0}', id="false-vs-0"),
    pytest.param('{"x":true}', '{"x":1.0}', id="true-vs-1.0"),
    pytest.param('{"x":false}', '{"x":0.0}', id="false-vs-0.0"),
    pytest.param('{"x":false}', '{"x":-0}', id="false-vs-negative-zero"),
    pytest.param('{"x":true}', '{"x":false}', id="true-vs-false"),
    pytest.param('{"x":null}', '{"x":false}', id="null-vs-false"),
    pytest.param('{"x":true}', '{"x":"true"}', id="bool-vs-string"),
    pytest.param('{"x":[true]}', '{"x":[1]}', id="bool-in-array"),
    pytest.param('{"n":{"a":[1,{"b":false}]}}', '{"n":{"a":[1,{"b":0}]}}', id="nested-bool"),
    pytest.param('{"x":[1,2]}', '{"x":[2,1]}', id="array-order"),
    pytest.param('{"x":[true,1]}', '{"x":[1,true]}', id="array-order-bool"),
]


@pytest.mark.parametrize(("a", "b"), EQUIVALENT)
def test_numerically_equal_payloads_are_the_same_event(a: str, b: str) -> None:
    assert same(a, b) and same(b, a)


@pytest.mark.parametrize(("a", "b"), DISTINCT)
def test_distinct_payload_values_are_different_events(a: str, b: str) -> None:
    assert not same(a, b) and not same(b, a)


def test_numbers_are_parsed_exactly() -> None:
    key = rec(f'{{"f":0.10000000000000000001,"i":{BIG},"e":1E+400}}').content_key()
    raw = key[-1]
    assert raw == {"f": D("0.10000000000000000001"), "i": int(BIG), "e": D("1E+400")}
    assert isinstance(raw["f"], Decimal) and isinstance(raw["i"], int)


def test_booleans_are_tagged_and_numbers_left_exact() -> None:
    raw = rec('{"t":true,"f":false,"n":null,"a":[1,true]}').content_key()[-1]
    assert raw == {"t": (bool, True), "f": (bool, False), "n": None, "a": [1, (bool, True)]}
    assert raw != {"t": 1, "f": 0, "n": None, "a": [1, 1]}


@pytest.mark.parametrize(
    ("stored", "replay", "equal"),
    [(D("-0.02"), D("-0.020"), True), (D("-0.02"), D("-0.02000000000000000001"), False),
     (D("-0.02"), None, False)],
)  # fmt: skip
def test_typed_money_comparison_is_unchanged(
    stored: Decimal, replay: Decimal | None, equal: bool
) -> None:
    raw = '{"id":"x"}'
    assert (rec(raw, stored).content_key() == rec(raw, replay).content_key()) is equal


# ============================================================ property
def json_eq(a: Any, b: Any) -> bool:
    """Reference JSON value equality written independently of the production code: the
    same JSON type on both sides (bool, number, string, null, array, object), numbers by
    exact value, arrays in order, objects regardless of key order."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a is b
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        return D(a) == D(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_eq(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b  # str, None


def test_reference_equality_sanity() -> None:
    assert json_eq({"a": [D("1.0"), True]}, {"a": [1, True]})
    assert not json_eq(True, 1) and not json_eq(0, False) and not json_eq(None, False)
    assert not json_eq("1", 1) and not json_eq([1, 2], [2, 1])


# A small value domain so that equal and unequal pairs both occur often.
NUMBERS = [D("0.1"), D("0.10"), D("0.10000000000000000001"), D("1E+400"), D("2E+400"),
           D("1.0"), 1, 10**30, 10**30 + 1, D("-0"), 0, D("0.0")]  # fmt: skip
STRINGS = ["0.1", "0.10", "1", "", "true"]
BOOLS = [True, False]
leaves = st.sampled_from(NUMBERS) | st.sampled_from(STRINGS) | st.sampled_from(BOOLS) | st.none()
values = st.recursive(
    leaves,
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.sampled_from(["a", "b", "c"]), inner, max_size=3)
    ),
    max_leaves=6,
)
payloads = st.dictionaries(st.sampled_from(["x", "y"]), values, min_size=1, max_size=2)


@settings(max_examples=300)
@given(payloads, payloads)
def test_key_equality_is_exact_json_value_equality(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Key equality is exactly the reference JSON value equality :func:`json_eq` (exact
    numbers, booleans distinct from numbers, order-free objects, ordered arrays).
    Payloads go through the production encoder, as stored and as re-received."""
    text_a, text_b = raw_json(a), raw_json(b)
    assert same(text_a, text_b) is json_eq(a, b)
    assert json_eq(json.loads(text_a, parse_float=Decimal), a)  # the encoding is exact


# Paired payloads: the same shape on both sides, each leaf either identical or a "twin"
# (equal or deliberately unequal), so near-collisions are drawn on purpose.
TWINS: list[tuple[Any, Any]] = [
    (D("0.1"), D("0.10000000000000000001")),  # unequal below float precision
    (D("1E+400"), D("2E+400")),  # unequal beyond float range
    (D("1E-400"), D("2E-400")),  # unequal below float range
    (10**30, 10**30 + 1),  # unequal big ints
    ("0.1", D("0.1")),  # string vs number
    ("0.1", "0.10"),  # string spellings
    (D("0.1"), D("0.10")),  # equal: trailing zero
    (D("1.0"), 1),  # equal: integral fraction vs int
    (D("-0"), 0),  # equal: negative zero
    (True, 1),  # boolean vs number (Step 15B.6C.1)
    (False, 0),
    (True, D("1.0")),
    (False, D("0.0")),
    (False, D("-0")),
    (True, False),
    (None, False),  # null vs false
    (True, "true"),  # boolean vs string
]
LEAF_PAIRS = (
    [(v, v) for v in [*NUMBERS, *STRINGS, *BOOLS, None]] + TWINS + [(b, a) for a, b in TWINS]
)


def _split_list(pairs: list[tuple[Any, Any]]) -> tuple[Any, Any]:
    return [a for a, _ in pairs], [b for _, b in pairs]


def _split_dict(pairs: dict[str, tuple[Any, Any]]) -> tuple[Any, Any]:
    return {k: a for k, (a, _) in pairs.items()}, {k: b for k, (_, b) in pairs.items()}


value_pairs = st.recursive(
    st.sampled_from(LEAF_PAIRS),
    lambda inner: (
        st.lists(inner, max_size=3).map(_split_list)
        | st.dictionaries(st.sampled_from(["a", "b", "c"]), inner, max_size=3).map(_split_dict)
    ),
    max_leaves=6,
)
payload_pairs = st.dictionaries(st.sampled_from(["x", "y"]), value_pairs, min_size=1,
                                max_size=2).map(_split_dict)  # fmt: skip


@settings(max_examples=300)
@given(payload_pairs)
def test_key_equality_is_exact_for_near_twin_payloads(pair: tuple[Any, Any]) -> None:
    a, b = pair
    assert same(raw_json(a), raw_json(b)) is json_eq(a, b)
