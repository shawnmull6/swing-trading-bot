"""Step 2: secret-safe structured logging."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.observability.logging import (
    REDACTED,
    SecretRedactor,
    configure_logging,
)

FAKE_KEY = "PKFAKETESTKEY000000001"
FAKE_SECRET = "fakeTestSecretValue/DoNotUse/0000000000001"


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    logger = logging.getLogger("test.redaction")
    logger.propagate = False
    configure_logging(secrets=[FAKE_KEY, FAKE_SECRET], stream=stream, logger=logger)
    yield logger, stream
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logging.captureWarnings(capture=False)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def _assert_clean(stream: io.StringIO) -> None:
    text = stream.getvalue()
    assert FAKE_KEY not in text
    assert FAKE_SECRET not in text


def test_secret_in_message_is_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info("connecting with %s and %s", FAKE_KEY, FAKE_SECRET)
    _assert_clean(stream)
    (line,) = _lines(stream)
    assert line["message"] == f"connecting with {REDACTED} and {REDACTED}"
    assert line["level"] == "INFO"


def test_secret_in_exception_is_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    try:
        raise RuntimeError(f"auth failed for {FAKE_SECRET}")
    except RuntimeError:
        logger.exception("broker error")
    _assert_clean(stream)
    assert "RuntimeError" in str(_lines(stream)[0]["exc"])


def test_secret_in_extra_fields_is_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info(
        "request",
        extra={
            "headers": {"APCA-API-KEY-ID": "any-value", "Accept": "json"},
            "api_key": "unknown-but-sensitive",
            "note": f"embedded {FAKE_KEY}",
            "count": 3,
        },
    )
    _assert_clean(stream)
    extra = _lines(stream)[0]["extra"]
    assert isinstance(extra, dict)
    assert extra["headers"] == {"APCA-API-KEY-ID": REDACTED, "Accept": "json"}
    assert extra["api_key"] == REDACTED
    assert extra["note"] == f"embedded {REDACTED}"
    assert extra["count"] == 3


@pytest.mark.parametrize(
    "text",
    [
        "ALPACA_SECRET_KEY=abc123def",
        'secret_key: "abc123def"',
        "Authorization=Bearer-abc123def",
        "{'api_key': 'abc123def'}",
    ],
)
def test_sensitive_key_value_patterns_redacted_even_if_unknown(text: str) -> None:
    out = SecretRedactor().redact(text)
    assert "abc123def" not in out
    assert REDACTED in out


def test_bad_format_arguments_do_not_leak(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info("only one placeholder %s", FAKE_KEY, FAKE_SECRET)  # noqa: PLE1205
    _assert_clean(stream)
    assert "suppressed" in str(_lines(stream)[0]["message"])


def test_configure_replaces_existing_handlers() -> None:
    logger = logging.getLogger("test.replace")
    logger.addHandler(logging.StreamHandler(io.StringIO()))
    handler = configure_logging(secrets=[], stream=io.StringIO(), logger=logger)
    assert logger.handlers == [handler]
    logger.removeHandler(handler)
    logging.captureWarnings(capture=False)


def test_overlapping_secrets_longest_first() -> None:
    r = SecretRedactor(["abcd", "abcdefgh"])
    assert r.redact("x abcdefgh y") == f"x {REDACTED} y"


@given(prefix=st.text(max_size=20), suffix=st.text(max_size=20))
def test_known_secret_never_survives(prefix: str, suffix: str) -> None:
    r = SecretRedactor([FAKE_SECRET])
    assert FAKE_SECRET not in r.redact(prefix + FAKE_SECRET + suffix)
