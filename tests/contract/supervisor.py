"""PYTEST_DONT_REWRITE

Shared supervision for the live read-only contract run (Step 15A.1: R1, R2, R5, R6).

* :class:`RequestBudget` — one budget for the whole run (setup, broker-check and every
  contract check): at most ``max_requests`` actual HTTP attempts (retries included) and a
  ``deadline_s`` measured on a monotonic clock. Exhaustion raises
  :class:`BudgetExhausted`, a ``BaseException`` so no ``except Exception`` / ``except
  BrokerError`` in the code under test can swallow it; once exhausted, every later request
  is refused before it is sent.
* :class:`BudgetedSession` — a GET-only, allowlisted ``requests`` session wrapper. Each
  attempt is counted *before* it is sent; connect/read timeouts are cut to the remaining
  deadline; response metadata is reduced to an allowlist of rate-limit headers; raw
  bodies are kept only for the local evidence directory, with secret values redacted.
* :func:`bounded_sleep` — retry backoff that never sleeps past the deadline.
* :class:`EvidenceWriter` — 0700 directories, 0600 files, no symlinks, no pre-existing
  destinations, and (by default) only inside the Git-ignored ``contract-evidence/``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS
from swing_trading_bot.observability.logging import SecretRedactor

REPO: Final = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT: Final = REPO / "contract-evidence"
MAX_REQUESTS: Final = 120
DEADLINE_S: Final = 300.0
RATE_HEADERS: Final = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                       "Retry-After")  # fmt: skip
FORBIDDEN_VERBS: Final = frozenset({"post", "put", "patch", "delete", "request", "send", "head",
                                    "options"})  # fmt: skip


class BudgetExhausted(BaseException):
    """The run's request or time budget is spent. ``code`` is a fixed diagnostic."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WriteAttemptError(BaseException):
    """A non-GET method or a non-allowlisted destination was attempted."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RequestBudget:
    max_requests: int = MAX_REQUESTS
    deadline_s: float = DEADLINE_S
    monotonic: Callable[[], float] = time.monotonic
    used: int = 0
    by_phase: dict[str, int] = field(default_factory=dict)
    phase: str = "setup"
    exhausted: str | None = None
    _start: float | None = None

    def start(self) -> None:
        if self._start is None:
            self._start = self.monotonic()

    def elapsed(self) -> float:
        self.start()
        assert self._start is not None
        return self.monotonic() - self._start

    def remaining(self) -> float:
        return self.deadline_s - self.elapsed()

    def _stop(self, code: str) -> BudgetExhausted:
        self.exhausted = self.exhausted or code
        return BudgetExhausted(self.exhausted)

    def check_time(self) -> float:
        left = self.remaining()
        if self.exhausted is not None:
            raise BudgetExhausted(self.exhausted)
        if left <= 0:
            raise self._stop("DEADLINE_EXCEEDED")
        return left

    def take(self) -> float:
        """Count one HTTP attempt before it is sent; returns the seconds left."""
        left = self.check_time()
        if self.used >= self.max_requests:
            raise self._stop("REQUEST_BUDGET_EXCEEDED")
        self.used += 1
        self.by_phase[self.phase] = self.by_phase.get(self.phase, 0) + 1
        return left


def bounded_sleep(budget: RequestBudget, sleep: Callable[[float], None] = time.sleep
                  ) -> Callable[[timedelta], None]:  # fmt: skip
    """A ``Sleeper`` for the adapter's retry backoff that never sleeps past the deadline."""

    def wait(delay: timedelta) -> None:
        seconds = delay.total_seconds()
        if seconds >= budget.check_time():
            raise budget._stop("DEADLINE_EXCEEDED")
        sleep(seconds)

    return wait


@dataclass
class Exchange:
    method: str
    host: str
    path: str
    status: int
    rate_headers: dict[str, str]
    body: str = field(repr=False)


class BudgetedSession:
    """GET-only, allowlisted, budgeted. Wraps any object with ``get(url, **kw)``."""

    def __init__(self, inner: Any, budget: RequestBudget, redactor: SecretRedactor) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "_redactor", redactor)
        object.__setattr__(self, "exchanges", [])

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_VERBS:
            raise WriteAttemptError("WRITE_METHOD_ATTEMPTED")
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def get(self, url: str, **kwargs: Any) -> Any:
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" not in ALLOWED_ORIGINS.values():
            raise WriteAttemptError("NON_ALLOWLISTED_DESTINATION")
        if kwargs.get("allow_redirects", True):
            raise WriteAttemptError("REDIRECTS_NOT_DISABLED")
        left = self.budget.take()
        connect, read = kwargs.get("timeout") or (left, left)
        kwargs["timeout"] = (min(float(connect), left), min(float(read), left))
        try:
            response = self._inner.get(url, **kwargs)
        except Exception:
            self.exchanges.append(Exchange("GET", parts.netloc, parts.path, -1, {}, ""))
            self.budget.check_time()  # a timeout that consumed the deadline stops the run
            raise
        headers = getattr(response, "headers", {}) or {}
        kept = {h: str(headers[h]) for h in RATE_HEADERS if h in headers}
        self.exchanges.append(Exchange("GET", parts.netloc, parts.path,
                                       int(response.status_code), kept,
                                       self._redactor.redact(response.text)))  # fmt: skip
        self.budget.check_time()
        return response


# ============================================================ rate-limit evidence (R2)
def _int(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


def rate_limit_evidence(exchanges: Iterable[Exchange]) -> dict[str, Any]:
    """Per host: what the rate-limit headers showed. ``OBSERVED`` only when every response
    carried all three X-RateLimit headers as integers with one consistent limit and
    ``remaining <= limit``; otherwise ``UNVERIFIED`` with a fixed reason. No limit is
    assumed when headers are absent."""
    out: dict[str, Any] = {}
    by_host: dict[str, list[Exchange]] = {}
    for ex in exchanges:
        if ex.status >= 0:
            by_host.setdefault(ex.host, []).append(ex)
    for host, items in sorted(by_host.items()):
        limits, remaining, missing, malformed = set(), [], 0, 0
        for ex in items:
            values = [ex.rate_headers.get(h) for h in RATE_HEADERS[:3]]
            if any(v is None for v in values):
                missing += 1
                continue
            parsed = [_int(v) for v in values]
            if any(p is None for p in parsed):
                malformed += 1
                continue
            limit, left, _reset = parsed
            limits.add(limit)
            remaining.append(left)
            if left is not None and limit is not None and left > limit:
                malformed += 1
        reason = None
        if missing:
            reason = "HEADERS_MISSING"
        elif malformed:
            reason = "HEADERS_MALFORMED"
        elif len(limits) != 1:
            reason = "LIMIT_INCONSISTENT"
        out[host] = {
            "responses": len(items),
            "status": "UNVERIFIED" if reason else "OBSERVED",
            "reason": reason,
            "limit": next(iter(limits)) if len(limits) == 1 and not reason else None,
            "min_remaining": min((r for r in remaining if r is not None), default=None),
            "responses_missing_headers": missing,
            "responses_malformed_headers": malformed,
            "retry_after_seen": sum(1 for ex in items if "Retry-After" in ex.rate_headers),
            "http_429_seen": sum(1 for ex in items if ex.status == 429),
        }
    return out


# ============================================================ evidence (R5)
class EvidenceError(RuntimeError):
    pass


def assert_sanitized(summary: Mapping[str, Any], forbidden: Iterable[str]) -> str:
    """Serialize ``summary``; refuse if any forbidden value appears in it."""
    text = json.dumps(summary, indent=2, sort_keys=True)
    leaked = [i for i, value in enumerate(forbidden) if value and len(value) >= 4
              and value in text]  # fmt: skip
    if leaked:
        raise AssertionError(f"sanitized summary contains forbidden value(s) #{leaked}")
    return text


def _git_ignored(path: Path) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "check-ignore", "-q", str(path)],  # noqa: S607
        cwd=REPO, capture_output=True, timeout=5, check=False,
    )  # fmt: skip
    return result.returncode == 0


class EvidenceWriter:
    """Raw bodies → ``<root>/<run>/raw/`` and ``summary.json`` beside them. Directories
    0700, files 0600 (``O_EXCL | O_NOFOLLOW``); symlinks and existing destinations are
    refused; by default the root must be Git-ignored."""

    def __init__(self, root: Path = EVIDENCE_ROOT, *, run: str | None = None,
                 require_git_ignored: bool = True) -> None:  # fmt: skip
        self.root = root
        self.run = run or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        self.require_git_ignored = require_git_ignored

    @property
    def dir(self) -> Path:
        return self.root / self.run

    def _prepare(self) -> Path:
        if not self.root.is_absolute() or "/" in self.run or self.run in {"", ".", ".."}:
            raise EvidenceError("EVIDENCE_PATH_INVALID")
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise EvidenceError("EVIDENCE_ROOT_NOT_A_DIRECTORY")
        if self.require_git_ignored and not _git_ignored(self.root / "probe"):
            raise EvidenceError("EVIDENCE_ROOT_NOT_GIT_IGNORED")
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        try:
            os.mkdir(self.dir, 0o700)
        except FileExistsError:
            raise EvidenceError("EVIDENCE_DESTINATION_EXISTS") from None
        os.chmod(self.dir, 0o700)
        os.mkdir(self.dir / "raw", 0o700)
        os.chmod(self.dir / "raw", 0o700)
        if any(stat.S_ISLNK(os.lstat(p).st_mode) for p in (self.root, self.dir, self.dir / "raw")):
            raise EvidenceError("EVIDENCE_SYMLINK")  # pragma: no cover - checked above
        return self.dir

    @staticmethod
    def _write(path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o600)

    def write(self, exchanges: Iterable[Exchange], summary: Mapping[str, Any],
              forbidden: Iterable[str]) -> Path:  # fmt: skip
        text = assert_sanitized(summary, list(forbidden))
        target = self._prepare()
        for n, ex in enumerate(exchanges):
            name = f"{n:03d}-{ex.host.split('.')[0]}{ex.path.replace('/', '_')}.json"
            self._write(target / "raw" / name, json.dumps({"status": ex.status, "body": ex.body}))
        self._write(target / "summary.json", text)
        return target / "summary.json"


SENSITIVE_KEYS: Final = (
    "id", "account_number", "cash", "equity", "buying_power", "non_marginable_buying_power",
    "portfolio_value", "last_equity", "symbol", "qty", "market_value", "order_id",
    "client_order_id",
)  # fmt: skip


def forbidden_values(exchanges: Iterable[Exchange], secrets: Iterable[str]) -> list[str]:
    """Values that must never appear in a sanitized summary: the secrets plus every
    account identifier, account number, balance, position and activity id the run saw."""
    values: set[str] = {s for s in secrets if s}
    for ex in exchanges:
        try:
            body = json.loads(ex.body) if ex.body else None
        except ValueError:
            continue
        rows = body if isinstance(body, list) else [body]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in SENSITIVE_KEYS:
                value = row.get(key)
                if isinstance(value, str) and len(value) >= 4:
                    values.add(value)
    return sorted(values)
