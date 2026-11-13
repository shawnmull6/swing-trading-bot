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
  deadline; response metadata is reduced to an allowlist of rate-limit headers; the
  original body is kept **in memory only** (never logged, never in ``repr``).
* :func:`bounded_sleep` — retry backoff that never sleeps past the deadline.
* :class:`EvidenceWriter` — 0700 directories, 0600 files, no symlinks, no pre-existing
  destinations, and (by default) only inside the Git-ignored ``contract-evidence/``.
* Evidence sanitization (Step 15B.5C) — :func:`sanitize_body` parses JSON bodies into a
  structure (exact ``Decimal`` numbers, ``null``/booleans/arrays/objects preserved),
  redacts by an explicit key/value policy, serializes canonically and verifies the result;
  non-JSON text gets exact-value replacement only. Every failure is a fixed-code
  :class:`SanitizationError` raised **before** anything is written.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from secrets import token_hex
from typing import Any, Final
from urllib.parse import urlsplit

from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS

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
    body: str = field(repr=False)  # the ORIGINAL response text: in memory only, never
    # logged; persisted solely through sanitize_body() (Step 15B.5C)


class BudgetedSession:
    """GET-only, allowlisted, budgeted. Wraps any object with ``get(url, **kw)``."""

    def __init__(self, inner: Any, budget: RequestBudget) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "budget", budget)
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
                                       response.text))  # fmt: skip
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


# ============================================================ evidence (R5, 15B.5C)
class EvidenceError(RuntimeError):
    pass


class SanitizationError(Exception):
    """Evidence could not be proven sanitized. ``code`` is a fixed diagnostic; the message
    never contains response content, a value or an index into one."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


REDACTED: Final = "***REDACTED***"
#: Keys whose string/number values are always hidden (a credential could sit there).
#: ``null`` and booleans under them carry nothing and are kept (``next_page_token: null``).
CREDENTIAL_KEY: Final = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|apca-api-key-id|"
    r"apca-api-secret-key|credential)"
)
#: Account identifiers: hidden by key anywhere, and their values everywhere (exact match).
ACCOUNT_ID_KEYS: Final = frozenset({"account_number", "account_id"})
#: Responses whose top-level ``id`` is the account identifier.
ACCOUNT_PATHS: Final = frozenset({"/v2/account"})
#: Summary-only sensitive keys (any depth): never in ``summary.json``; private raw
#: evidence may keep them (balances, positions, activity/order ids).
SENSITIVE_KEYS: Final = (
    "id", "account_number", "cash", "equity", "buying_power", "non_marginable_buying_power",
    "portfolio_value", "last_equity", "symbol", "qty", "market_value", "order_id",
    "client_order_id",
)  # fmt: skip
_NOT_JSON: Final = object()
#: Deepest nesting accepted in evidence (Alpaca payloads are a few levels deep); deeper is
#: refused explicitly instead of risking recursion limits in the redactor.
MAX_EVIDENCE_DEPTH: Final = 64


def _reject_constant(_name: str) -> object:
    raise ValueError("non-JSON constant")


def _expects_json(ex: Exchange) -> bool:
    return 200 <= ex.status < 300


def parse_body(ex: Exchange) -> object:
    """The body as JSON (``Decimal`` for fractions/exponents; ``NaN``/``Infinity``
    rejected), or ``_NOT_JSON``. A successful response is expected to be JSON: if it is
    not, that is :class:`SanitizationError` — it is never skipped silently."""
    if not ex.body:
        return _NOT_JSON
    try:
        doc = json.loads(ex.body, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        if _expects_json(ex):
            raise SanitizationError("EVIDENCE_JSON_MALFORMED") from None
        return _NOT_JSON
    _check_depth(doc)
    return doc


def _check_depth(doc: object) -> None:
    stack: list[tuple[object, int]] = [(doc, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > MAX_EVIDENCE_DEPTH:
                raise SanitizationError("EVIDENCE_TOO_DEEP")
            items = value.values() if isinstance(value, dict) else value
            stack.extend((item, depth + 1) for item in items)


def _pairs(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _pairs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _pairs(v)


def identity_values(exchanges: Iterable[Exchange], secrets: Iterable[str]) -> list[str]:
    """Values that must never be persisted anywhere: the credentials plus every account
    identifier (``account_number``/``account_id`` at any depth, the account ``id``)."""
    values: set[str] = {s for s in secrets if s}
    for ex in exchanges:
        doc = parse_body(ex)
        if doc is _NOT_JSON:
            continue
        values.update(v for k, v in _pairs(doc)
                      if k in ACCOUNT_ID_KEYS and isinstance(v, str) and v)  # fmt: skip
        if ex.path in ACCOUNT_PATHS and isinstance(doc, dict):
            account_id = doc.get("id")
            if isinstance(account_id, str) and account_id:
                values.add(account_id)
    return sorted(values, key=lambda v: (-len(v), v))  # longest first: overlaps fully masked


def _summary_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    return None


def forbidden_values(exchanges: Iterable[Exchange], secrets: Iterable[str]) -> list[str]:
    """Values that must never appear in a sanitized summary, collected from the
    **original** bodies: the identity values plus every value of a :data:`SENSITIVE_KEYS`
    key at any depth (balances, positions, activity ids, ...). Malformed expected-JSON
    evidence raises :class:`SanitizationError` instead of being skipped."""
    items = list(exchanges)
    values = set(identity_values(items, secrets))
    for ex in items:
        doc = parse_body(ex)
        if doc is _NOT_JSON:
            continue
        for key, raw in _pairs(doc):
            value = _summary_value(raw) if key in SENSITIVE_KEYS else None
            if value is not None and len(value) >= 4:
                values.add(value)
    return sorted(values)


def _scrub(text: str, identities: Sequence[str]) -> str:
    for value in identities:
        text = text.replace(value, REDACTED)
    return text


def _redact(value: object, identities: Sequence[str], hide: bool) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            new_key = _scrub(key, identities)
            if new_key in out:
                raise SanitizationError("EVIDENCE_KEY_COLLISION")
            sensitive = hide or bool(CREDENTIAL_KEY.search(key)) or key in ACCOUNT_ID_KEYS
            out[new_key] = _redact(item, identities, sensitive)
        return out
    if isinstance(value, list):
        return [_redact(item, identities, hide) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if hide and isinstance(value, (str, int, Decimal)):
        return REDACTED
    if isinstance(value, str):
        return _scrub(value, identities)
    return value


def canonical_json(value: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, exact ``Decimal``
    numbers. NaN/Infinity and any type JSON cannot represent raise
    :class:`SanitizationError` — nothing is coerced (no ``default=str``)."""
    parts: list[str] = []
    _encode(value, parts)
    return "".join(parts)


def _encode(value: object, out: list[str]) -> None:
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise SanitizationError("EVIDENCE_UNSUPPORTED_TYPE")
        out.append("{")
        for n, key in enumerate(sorted(value)):
            out.append("," if n else "")
            out.append(json.dumps(key) + ":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, list):
        out.append("[")
        for n, item in enumerate(value):
            out.append("," if n else "")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise SanitizationError("EVIDENCE_NON_FINITE_NUMBER")
        out.append(str(value))
    elif value is None or isinstance(value, (str, int)):  # bool is an int
        out.append(json.dumps(value))
    else:  # float (never produced by parse_body), objects, sets, bytes, ...
        raise SanitizationError("EVIDENCE_UNSUPPORTED_TYPE")


def _verify_absent(text: str, identities: Iterable[str]) -> None:
    for value in identities:
        if value in text or json.dumps(value)[1:-1] in text:
            raise SanitizationError("EVIDENCE_FORBIDDEN_VALUE")


def sanitize_body(ex: Exchange, identities: Sequence[str]) -> str:
    """The persisted form of one exchange: ``{"body": <text>, "status": <int>}`` where a
    JSON body is the canonical, redacted JSON text (still valid JSON) and a non-JSON body
    has every identity value replaced exactly. Verified: no identity value — raw or
    JSON-escaped — survives."""
    doc = parse_body(ex)
    if doc is _NOT_JSON:
        body = _scrub(ex.body, identities)
    else:
        body = canonical_json(_redact(doc, identities, hide=False))
    text = json.dumps({"status": ex.status, "body": body})
    _verify_absent(body, identities)
    _verify_absent(text, identities)
    return text


def assert_sanitized(summary: Mapping[str, Any], forbidden: Iterable[str]) -> str:
    """Serialize ``summary``; refuse if any forbidden value appears in it."""
    try:
        text = json.dumps(summary, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        raise SanitizationError("SUMMARY_UNSERIALIZABLE") from None
    if any(value and len(value) >= 4 and value in text for value in forbidden):
        raise SanitizationError("SUMMARY_FORBIDDEN_VALUE")
    return text


def _git_ignored(path: Path) -> bool:
    """``False`` (fail closed) when Git says no **or** cannot answer (missing, timeout)."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "check-ignore", "-q", str(path)],  # noqa: S607
            cwd=REPO, capture_output=True, timeout=5, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class EvidenceWriteError(EvidenceError):
    """A filesystem operation failed while persisting evidence (Step 15B.5D). The message
    is the fixed code only — never a path, errno text or content. ``staging_removed`` is
    ``False`` when an incomplete ``.incomplete-*`` staging directory had to be left."""

    CODE: Final = "EVIDENCE_WRITE_FAILED"

    def __init__(self, *, staging_removed: bool) -> None:
        super().__init__(self.CODE)
        self.code = self.CODE
        self.staging_removed = staging_removed


class EvidenceCollisionError(EvidenceError):
    """The run's destination appeared after the pre-check: ``rename`` refused to replace a
    non-empty directory. A collision (``EVIDENCE_DESTINATION_EXISTS``), not an I/O fault."""

    def __init__(self, *, staging_removed: bool) -> None:
        super().__init__("EVIDENCE_DESTINATION_EXISTS")
        self.staging_removed = staging_removed


#: Staging directories start with this; run names may not start with ``.``, so a staging
#: (or abandoned incomplete) directory can never be mistaken for a completed run.
STAGING_PREFIX: Final = ".incomplete-"
SUMMARY_NAME: Final = "summary.json"


# ============================================================ atomic no-replace rename (15B.5D)
#: macOS ``<stdio.h>`` (10.12+): ``int renamex_np(const char *from, const char *to,
#: unsigned int flags)``; ``RENAME_EXCL`` fails with ``EEXIST`` if ``to`` exists.
RENAME_EXCL: Final = 0x00000004
#: Linux ``<linux/fs.h>`` / glibc ``<stdio.h>`` (kernel 3.15+, glibc 2.28+):
#: ``int renameat2(int olddirfd, const char *oldpath, int newdirfd, const char *newpath,
#: unsigned int flags)``; ``RENAME_NOREPLACE`` fails with ``EEXIST`` if ``newpath`` exists.
RENAME_NOREPLACE: Final = 0x00000001
AT_FDCWD: Final = -100
#: ``(src, dst) -> 0`` on success, ``-1`` with ``errno`` set on failure (C convention).
NativeRename = Callable[[bytes, bytes], int]
#: errno values that mean "the destination already exists" (a collision).
COLLISION_ERRNOS: Final = frozenset({errno.EEXIST, errno.ENOTEMPTY})


def resolve_noreplace_rename(platform: str, lib: Any = None) -> NativeRename | None:
    """The platform's atomic no-replace rename, or ``None`` (unsupported OS or missing
    symbol — the caller fails closed; there is no ordinary-rename fallback)."""
    if platform == "darwin":
        name = "renamex_np"
    elif platform.startswith("linux"):
        name = "renameat2"
    else:
        return None
    try:
        libc = lib if lib is not None else ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - the C library is always loadable on these OSes
        return None
    fn = getattr(libc, name, None)
    if fn is None:
        return None
    fn.restype = ctypes.c_int
    if name == "renamex_np":
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        return lambda src, dst: int(fn(src, dst, RENAME_EXCL))
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    return lambda src, dst: int(fn(AT_FDCWD, src, AT_FDCWD, dst, RENAME_NOREPLACE))


@functools.cache
def platform_noreplace_rename() -> NativeRename | None:
    return resolve_noreplace_rename(sys.platform)


def noreplace_rename(src: Path, dst: Path, native: NativeRename | None) -> None:
    """Atomically rename ``src`` to ``dst`` iff ``dst`` does not exist at that instant
    (empty directories and files included). ``OSError`` on failure: ``EEXIST`` for a
    collision; ``ENOSYS`` when no native call is available; otherwise the native errno
    (``EINVAL``/``ENOTSUP`` for a filesystem without no-replace support). The message is
    ``strerror`` only — never a path."""
    if native is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    src_b, dst_b = os.fsencode(src), os.fsencode(dst)
    if b"\0" in src_b or b"\0" in dst_b:  # c_char_p would silently truncate at NUL
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
    ctypes.set_errno(0)
    if native(src_b, dst_b) != 0:
        err = ctypes.get_errno() or errno.EIO
        raise OSError(err, os.strerror(err))


class FileOps:
    """The filesystem operations evidence persistence uses — one seam, so fault-injection
    tests can fail any single step without patching the global ``os`` module."""

    def mkdir(self, path: Path) -> None:
        os.mkdir(path, 0o700)

    def chmod(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)

    def open_new(self, path: Path) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def lstat(self, path: Path) -> os.stat_result:
        return os.lstat(path)

    def fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def rename(self, src: Path, dst: Path) -> None:
        """Atomic **no-replace** publication (renamex_np / renameat2); never os.rename."""
        noreplace_rename(src, dst, platform_noreplace_rename())

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path)  # never follows symlinks


def _raw_name(n: int, ex: Exchange) -> str:
    return f"{n:03d}-{ex.host.split('.')[0]}{ex.path.replace('/', '_')}.json"


class EvidenceWriter:
    """Raw bodies → ``<root>/<run>/raw/`` and ``summary.json`` beside them, published
    atomically (Step 15B.5D): everything is sanitized in memory, written and fsynced into a
    private ``<root>/.incomplete-<run>-<random>/`` staging directory, verified, and only
    then renamed to ``<root>/<run>``. Directories 0700, files 0600 (``O_EXCL |
    O_NOFOLLOW``); symlinks and existing destinations are refused; by default the root
    must be Git-ignored. Any filesystem failure is :class:`EvidenceWriteError`
    (``EVIDENCE_WRITE_FAILED``) and staging is removed where possible."""

    def __init__(self, root: Path = EVIDENCE_ROOT, *, run: str | None = None,
                 require_git_ignored: bool = True, fs: FileOps | None = None) -> None:  # fmt: skip
        self.root = root
        self.run = run or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        self.require_git_ignored = require_git_ignored
        self.fs = fs or FileOps()

    @property
    def dir(self) -> Path:
        return self.root / self.run

    def _check(self) -> None:
        """Refusals that need no write (unchanged codes)."""
        if (not self.root.is_absolute() or "/" in self.run or self.run in {"", ".", ".."}
                or self.run.startswith(".")):  # fmt: skip
            raise EvidenceError("EVIDENCE_PATH_INVALID")
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise EvidenceError("EVIDENCE_ROOT_NOT_A_DIRECTORY")
        if self.require_git_ignored and not _git_ignored(self.root / "probe"):
            raise EvidenceError("EVIDENCE_ROOT_NOT_GIT_IGNORED")
        if os.path.lexists(self.dir):
            raise EvidenceError("EVIDENCE_DESTINATION_EXISTS")

    def _private_dir(self, path: Path) -> None:
        self.fs.mkdir(path)
        self.fs.chmod(path, 0o700)

    def _write_file(self, path: Path, data: bytes) -> None:
        fd = self.fs.open_new(path)
        try:
            view = memoryview(data)
            while view:
                written = self.fs.write(fd, view.tobytes())
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            self.fs.fsync(fd)
        except BaseException:
            with contextlib.suppress(OSError):
                self.fs.close(fd)
            raise
        self.fs.close(fd)
        self.fs.chmod(path, 0o600)

    def _verify(self, staging: Path, files: list[tuple[Path, bytes]]) -> None:
        for path, data in files:
            st = self.fs.lstat(staging / path)
            if (not stat.S_ISREG(st.st_mode) or st.st_size != len(data)
                    or stat.S_IMODE(st.st_mode) != 0o600):  # fmt: skip
                raise OSError("evidence file incomplete")
        for d in (staging, staging / "raw"):
            st = self.fs.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700:
                raise OSError("evidence directory not private")

    def _publish(self, files: list[tuple[Path, bytes]]) -> Path:
        staging: Path | None = None
        try:
            if not self.root.exists():
                self._private_dir(self.root)
            self.fs.chmod(self.root, 0o700)
            staging = self.root / f"{STAGING_PREFIX}{self.run}-{token_hex(8)}"
            self._private_dir(staging)
            self._private_dir(staging / "raw")
            for path, data in files:
                self._write_file(staging / path, data)
            self.fs.fsync_dir(staging / "raw")
            self.fs.fsync_dir(staging)
            self._verify(staging, files)
            try:
                self.fs.rename(staging, self.dir)  # the single publication step
            except OSError as exc:
                if exc.errno in COLLISION_ERRNOS:
                    raise EvidenceCollisionError(staging_removed=self._cleanup(staging)) from None
                raise
            staging = None
            self.fs.fsync_dir(self.root)
        except OSError:
            raise EvidenceWriteError(staging_removed=self._cleanup(staging)) from None
        return self.dir / SUMMARY_NAME

    def _cleanup(self, staging: Path | None) -> bool:
        if staging is None or not os.path.lexists(staging):
            return True
        try:
            self.fs.rmtree(staging)
        except OSError:
            return False
        return not os.path.lexists(staging)

    def write(self, exchanges: Iterable[Exchange], summary: Mapping[str, Any],
              forbidden: Iterable[str], *, secrets: Iterable[str] = ()) -> Path:  # fmt: skip
        """Everything is sanitized and verified first (a :class:`SanitizationError` leaves
        nothing on disk), then published atomically (an :class:`EvidenceWriteError`
        never leaves a directory that looks like a completed run)."""
        items = list(exchanges)
        text = assert_sanitized(summary, list(forbidden))
        identities = identity_values(items, secrets)
        files = [(Path("raw") / _raw_name(n, ex), sanitize_body(ex, identities).encode())
                 for n, ex in enumerate(items)]  # fmt: skip
        files.append((Path(SUMMARY_NAME), text.encode()))
        self._check()
        return self._publish(files)
