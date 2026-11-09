"""Step 3: client order ids."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.ids import is_bot_client_order_id, new_client_order_id, uuid7


def test_uuid7_layout() -> None:
    u = uuid7(unix_ms=1_700_000_000_000, random_bytes=bytes(range(10)))
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"
    assert u.int >> 80 == 1_700_000_000_000


def test_uuid7_time_ordering() -> None:
    a = uuid7(unix_ms=1000, random_bytes=b"\xff" * 10)
    b = uuid7(unix_ms=1001, random_bytes=b"\x00" * 10)
    assert a < b


@pytest.mark.parametrize("ms", [-1, 1 << 48])
def test_uuid7_rejects_out_of_range_timestamp(ms: int) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        uuid7(unix_ms=ms)


def test_uuid7_rejects_wrong_random_length() -> None:
    with pytest.raises(ValueError, match="10 bytes"):
        uuid7(random_bytes=b"\x00")


def test_client_order_ids_are_unique_and_recognised() -> None:
    ids = {new_client_order_id() for _ in range(5000)}
    assert len(ids) == 5000
    assert all(is_bot_client_order_id(i) for i in ids)
    assert all(len(i) <= 128 for i in ids)  # Alpaca client_order_id limit


@pytest.mark.parametrize(
    "value",
    [
        "",
        "stb-",
        "manual-order-1",
        "STB-0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b",
        "stb-0190a3b2-7c4d-4e8f-9a0b-1c2d3e4f5a6b",  # v4, not v7
        "stb-0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b-extra",
    ],
)
def test_foreign_ids_not_recognised(value: str) -> None:
    assert not is_bot_client_order_id(value)


@given(st.integers(min_value=0, max_value=(1 << 48) - 1), st.binary(min_size=10, max_size=10))
def test_uuid7_always_well_formed(ms: int, rand: bytes) -> None:
    u = uuid7(unix_ms=ms, random_bytes=rand)
    assert u.version == 7
    assert is_bot_client_order_id(f"stb-{u}")
