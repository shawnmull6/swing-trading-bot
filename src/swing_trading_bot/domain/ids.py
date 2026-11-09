"""Identifier generation.

``client_order_id`` values are generated once per intent, persisted before any
network call, and never reused (ARCHITECTURE §10.2). UUIDv7 gives time-ordered ids;
Python 3.12 has no ``uuid.uuid7`` so a small RFC 9562 implementation lives here.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Final
from uuid import UUID

CLIENT_ORDER_ID_PREFIX: Final = "stb-"
_MAX_UNIX_MS: Final = (1 << 48) - 1
_CLIENT_ORDER_ID_RE: Final = re.compile(
    r"^stb-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def uuid7(*, unix_ms: int | None = None, random_bytes: bytes | None = None) -> UUID:
    """Return an RFC 9562 version-7 UUID.

    ``unix_ms`` and ``random_bytes`` exist for deterministic tests only.
    """
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    if not 0 <= ms <= _MAX_UNIX_MS:
        raise ValueError("timestamp out of range for UUIDv7")
    rand = secrets.token_bytes(10) if random_bytes is None else random_bytes
    if len(rand) != 10:
        raise ValueError("random_bytes must be exactly 10 bytes")
    rand_a = int.from_bytes(rand[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(rand[2:], "big") & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=value)


def new_client_order_id() -> str:
    return f"{CLIENT_ORDER_ID_PREFIX}{uuid7()}"


def is_bot_client_order_id(value: str) -> bool:
    """True only for ids this bot could have generated."""
    return bool(_CLIENT_ORDER_ID_RE.fullmatch(value))
