"""Structured JSON logging with secret redaction (ARCHITECTURE §13).

Redaction is applied twice (defence in depth):

1. :class:`RedactingFilter` on the handler rewrites each record: the fully formatted
   message, exception text, stack info and any ``extra`` fields.
2. :class:`JsonFormatter` redacts the final serialised line again.

Known secret values are replaced wherever they appear; in addition, values attached
to sensitive-looking keys (``api_key=...``, ``"secret": ...``, Alpaca auth headers)
are masked even if the value itself is not a known secret.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final, TextIO

REDACTED: Final = "***REDACTED***"

_SENSITIVE_KEY: Final = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|apca-api-key-id|"
    r"apca-api-secret-key|credential)"
)
_KEY_VALUE: Final = re.compile(
    r"""(?ix)
    (?P<key>[A-Za-z0-9_\-]*(?:api[_-]?key|secret|token|password|passwd|authorization|
        apca-api-key-id|apca-api-secret-key|credential)[A-Za-z0-9_\-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"',}]+)
    """
)

_STANDARD_RECORD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime"}
)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest first so overlapping secrets are fully masked.
        self._secrets: tuple[str, ...] = tuple(
            sorted({s for s in secrets if s}, key=len, reverse=True)
        )

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return _KEY_VALUE.sub(lambda m: f"{m['key']}{m['sep']}{REDACTED}", text)

    def redact_value(self, key: str, value: object) -> object:
        if _SENSITIVE_KEY.search(key):
            return REDACTED
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {str(k): self.redact_value(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(key, v) for v in value]
        if isinstance(value, (int, bool)) or value is None:
            return value
        return self.redact(str(value))


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        r = self._redactor
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not leak args
            message = f"{record.msg!s} [unformattable log arguments suppressed]"
        record.msg = r.redact(message)
        record.args = None
        if record.exc_info:
            record.exc_text = r.redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = r.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = r.redact(record.stack_info)
        for key in list(vars(record)):
            if key not in _STANDARD_RECORD_ATTRS:
                setattr(record, key, r.redact_value(key, getattr(record, key)))
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_RECORD_ATTRS}
        if extras:
            payload["extra"] = extras
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            payload["stack"] = record.stack_info
        line = json.dumps(payload, default=str, sort_keys=True)
        return self._redactor.redact(line)


def configure_logging(
    *,
    secrets: Iterable[str],
    level: int = logging.INFO,
    stream: TextIO | None = None,
    logger: logging.Logger | None = None,
) -> logging.Handler:
    """Install a single redacting JSON handler on ``logger`` (root by default).

    Existing handlers on that logger are removed so no unredacted path remains.
    """
    target = logger if logger is not None else logging.getLogger()
    redactor = SecretRedactor(secrets)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(RedactingFilter(redactor))
    handler.setFormatter(JsonFormatter(redactor))
    for existing in list(target.handlers):
        target.removeHandler(existing)
    target.addHandler(handler)
    target.setLevel(level)
    logging.captureWarnings(capture=True)
    return handler
